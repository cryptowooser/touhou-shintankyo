# th06c memory-editing worklog

Findings and tooling from reverse-engineering the Steam remaster of Touhou 6
(`th06c.exe`) to read and modify the in-game lives, bombs, and score counters.

Date: 2026-10-09.

## Result

The lives and bombs counters are two adjacent bytes in the executable's `.data`
section. Both are writable at runtime, and the HUD updates immediately.

| Field | RVA | Type | Notes |
|---|---|---|---|
| **bombs** | `0x3EE805` | signed byte | one HUD star per unit |
| **lives** | `0x3EE804` | signed byte | one HUD star per unit |
| score | `0x3ED7BC` | dword | displayed 得点; the HUD reads this |
| score accumulator | `0x3ED7C0` | dword | receives score additions; see below |
| high score | `0x3ED7C8` | dword | displayed 最高得点 |
| power | `0x3ED034` | word | HUD power bar, 0..128 |
| config lives | `0xBC28EC` | byte | `cfg_lives`; death path and stage init |
| config bombs | `0xBC28ED` | byte | `cfg_bombs`; **a death refills bombs from this** |
| **start_lives** | `0xBC28C4` | byte | read by the continue path |
| **start_bombs** | `0xBC28C5` | byte | read by the continue path |

Addresses are **RVAs**: offsets from the module base, stable across launches. The
module base moves with ASLR (observed at `0x7ff711bd0000`). Get the live address
with `modbase.find_module(pid, "th06c")`, then add the RVA.

Keep lives and bombs in `0..8`. The HUD loops once per unit and draws one sprite
each time. 8 is the game's own cap for bombs: the pickup increments behind a
`cmp byte ptr [0x3EE805], 8` / `jge` guard at `0x2B609`.

### Commands

```
cd th06c-tools
python gamestate.py show
python gamestate.py set bombs 8
python gamestate.py set lives 8
```

A death decrements lives and refills bombs from `cfg_bombs`, so a change to the
live counters does not survive one. Set all six fields to make it stick:

```
for f in lives bombs start_lives start_bombs cfg_lives cfg_bombs; do
  python gamestate.py set $f 8
done
```

`gamestate.py` bounds lives and bombs to `0..8` so a typo cannot drive the HUD
draw loop wild.

For experiments where the counters only need to stay high, run the watcher instead
of setting fields by hand:

```
python -u keepalive.py --interval 3
```

It reapplies 8 lives and 8 bombs whenever the game lowers them, which is what makes
lives effectively infinite across deaths.

## Methodology

How the counters were located, in the order that worked. This generalises to any
other value in this game.

### 1. Work from the executable on disk

Do not scan a running process to find code. `th06c.exe` is identical every launch
and only its base address moves, so every address can be derived read-only from the
file with no game running:

```
python disasm.py --secs
```

Sections, function boundaries, references, and disassembly all come from the file.
This is both safer and faster than guessing at live memory.

### 2. Anchor on a global whose value you can observe

The score was found first, by scanning writable memory for the value shown on
screen and confirming that writing it changed the display. That gave a known
global at RVA `0x3ED7BC` to hang everything else off. **Establish one anchor you
trust before looking for anything else.**

### 3. Find the code that reads the anchor

RIP-relative references locate the routines that touch a global:

```
python disasm.py --refs 0x3ED7BC
```

A reference is reported as the offset of the 32-bit displacement field, not the
instruction start, so disassemble the site before reading it:

```
python disasm.py --ctx 0x29993
```

This is what found the HUD stats routine at `0x297E6..0x29A0E`. Cluster the hits:
references that sit close together belong to one function, and the function that
reads your anchor is usually the one you want.

### 4. Read the surrounding code for neighbouring globals

HUD and state code touches related fields in sequence. Reading that routine is
what exposed `0x3ED034` (power) beside the score, and the byte pair at
`0x3EE804` / `0x3EE805` beside each other.

A one-instruction tell is worth more than a scan. The two adjacent counters were
spotted from:

```
0x2cc30  mov word ptr [0x3EE804], 0x302    ; lives=2, bombs=3 in one write
```

`0x0302` little-endian is the byte pair `02 03`. That is a stronger signal than any
value scan, because it shows the game itself treating them as a pair.

### 5. Corroborate from independent code paths

One reference is not proof. Each counter was confirmed by several unrelated paths
before anything was written:

| Path | RVA |
|---|---|
| HUD star row loop | `0x296F7`, `0x297AF` |
| Stage init from config | `0x91BA`, `0x91C7` |
| Stage-clear bonus, `lives * 3,000,000` | `0x27335` |
| Result screen save-back | `0x24312`, `0x2431F` |
| Bomb pickup, capped at 8 | `0x2B609` |
| Bomb use | `0x4A6B2` |
| Death: `lives - 1`, bombs refilled | `0x4AA71`, `0x4AA92` |
| Continue: restored from `start_*` | `0x2CC12`, `0x2CC1F` |

The stage-clear bonus is the clearest of these: a value multiplied by exactly
3,000,000 and added to the score is a remaining-life count, not a coincidence.

### 6. Verify at runtime, and verify the write landed

```
python gamestate.py show
python gamestate.py set bombs 8
```

**Read the value back and confirm it changed before drawing any conclusion.** A
write that silently fails looks exactly like a value the game overwrites, and
that confusion cost most of the time in this investigation. `write_mem` now raises
on failure for this reason.

### 7. Keep it applied

Anything the game restores on its own cannot be held by a single write. For
experimentation, run the watcher:

```
python -u keepalive.py --interval 3
```

It re-reads lives and bombs every few seconds and rewrites only what has drifted,
re-resolves the module base each tick (ASLR moves it on restart), and reattaches if
the game exits. Run it as a background task so it outlives the shell. Options:

```
keepalive.py --interval 3     # 8 lives and 8 bombs
keepalive.py --interval 0.5   # tighter, no visible dip after a death
keepalive.py --bombs 0        # lives only
keepalive.py --once           # apply once and exit
```

A death decrements lives and refills bombs from `cfg_bombs`, so the watcher is what
makes lives effectively infinite. For a durable change rather than a live one, set
all six fields instead (see the Commands section).

## Evidence for the field map

Each counter is confirmed by several independent code paths. All RVAs below are
in `th06c.exe`.

**The HUD star rows** (`0x296F7` lives, `0x29761` bombs). The row is drawn by
looping once per unit:

```
0x296f7  cmp   byte ptr [0x3EE804], r14b    ; lives
0x2973f  movsx eax, byte ptr [0x3EE804]
0x2974d  cmp   esi, eax
0x2974f  jl    0x29710                       ; draw another star
0x297af  movsx eax, byte ptr [0x3EE805]      ; bombs, same pattern
```

`movsx` means both are **signed** bytes.

**Stage init** (`0x91BA`, `0x91C7`) sets both from the config bytes:

```
0x91b3  movzx eax, byte ptr [0xBC28EC]
0x91ba  mov   byte ptr [0x3EE804], al
0x91c0  movzx eax, byte ptr [0xBC28ED]
0x91c7  mov   byte ptr [0x3EE805], al
```

**Stage-clear bonus** (`0x27335`), the clearest single proof:

```
0x27327  movsx eax, byte ptr [0x3EE804]
0x27335  imul  r9d, eax, 0x2dc6c0            ; lives * 3,000,000
0x2735b  movsx eax, byte ptr [0x3EE805]
```

3,000,000 points per remaining life is the standard Touhou stage-clear bonus.

**Result screen** (`0x24312`, `0x2431F`) writes both back to the config bytes.

**Bomb pickup** (`0x2B609`, `0x2B618`) increments bombs behind a cap of 8:

```
0x2b609  cmp  byte ptr [0x3EE805], 8
0x2b610  jge  0x2b62a                       ; skip the increment at the cap
0x2b618  inc  byte ptr [0x3EE805]
```

**Bomb use** (`0x4A6A7`/`0x4A6B2`):

```
0x4a6a7  dec  cl
0x4a6a9  inc  dword ptr [0x3ED014]
0x4a6b2  mov  byte ptr [0x3EE805], cl
```

**Respawn** (`0x2CC30`): `mov word ptr [0x3EE804], 0x302` — writes lives=2 and
bombs=3 in one instruction. `0x0302` little-endian is the byte pair `02 03`,
which is how the two adjacent counters were first spotted.

**Power** (`0x297F5`, `0x29813`): the bar draws up to 128 segments
(`mov ebx, 0x80`) with sprite index base `add eax, 0x1f0`.

The HUD stats **text** panel is a separate function at `0x297E6..0x29A0E`; it
formats the score from `0x3ED7BC` (`0x29990`) and the high score from `0x3ED7C8`
(`0x299C0`).

### Score additions target 0x3ED7C0, not 0x3ED7BC

Score gains are added to the accumulator at `0x3ED7C0`:

```
0x4c5ba  add dword ptr [rip + 0x3a11fc], 0x1f4    ; +500
0x4cb8d  add dword ptr [rip + 0x3a0c29], 0x1f4    ; +500
0x2b7e6  add dword ptr [rip + 0x3c1fd0], 0x3e8    ; +1000
```

The HUD reads `0x3ED7BC`, and the two values can differ during play (observed
414820 against 415800), so `0x3ED7BC` appears to be the displayed value and
`0x3ED7C0` the target it counts toward. **Set both when changing the score**, or
the accumulator will pull the display back.

## Counters are also copied to and from player records

The game copies the counters between the globals above and per-player record
objects, reading bytes `+9` (lives) and `+0xA` (bombs):

- `0x30837`, `0x3084D` — record → global, via `[[this+0x11610] + idx*8 + 0x38]`
- `0x4D67D`, `0x4DA78` — global → record, via `[[rbx+0x70] + idx*8 + 0x38]`
- `0x4DB91`, `0x4DB9C` — record → global, from a struct at `r8`

The **globals are authoritative**. Writing the globals is sufficient; the records
are updated from them.

### What resets lives and bombs

A death and a continue restore the counters by different paths, from different
sources. Both were observed.

**A normal death** decrements lives by one and refills bombs from `cfg_bombs`:

```
0x4aa6c  dec   al
0x4aa71  mov   byte ptr [0x3EE804], al     ; lives = lives - 1
0x4aa79  cmp   dword ptr [0x3ED7D8], 4
0x4aa80  jge   0x4aaa1
0x4aa82  cmp   byte ptr [0x3ED7D0], sil
0x4aa89  jne   0x4aaa9a
0x4aa8b  movzx eax, byte ptr [0xBC28ED]
0x4aa92  mov   byte ptr [0x3EE805], al     ; bombs = cfg_bombs
```

Observed: with lives and bombs at 8 and `cfg_bombs` at 3, a death left lives at 7
and bombs at 3.

**A continue** restores both from `start_lives` / `start_bombs`, via the function at
`0x2CC00..0x2CCE4`:

```
0x2cc04  cmp   dword ptr [0x3ED7D8], 4
0x2cc0b  movzx eax, byte ptr [0xBC28C4]
0x2cc12  mov   byte ptr [0x3EE804], al
0x2cc18  movzx eax, byte ptr [0xBC28C5]
0x2cc1f  mov   byte ptr [0x3EE805], al
0x2cc2e  je    0x2cc39
0x2cc30  mov   word ptr [0x3EE804], 0x302     ; forced lives=2, bombs=3
```

The main update calls it at `0x35958` and `0x3629B`. Observed: with lives and
bombs at 8 and this pair at 2/3, they came back as exactly 2 and 3 while the score
kept advancing.

The hardcoded `0x302` override at `0x2CC30` only runs when `[0x3ED7D8] == 4` or
`[0x3ED7D0] != 0`. Both were false on the observed continue (`[0x3ED7D8]` was 1,
`[0x3ED7D0]` was 0), so it was not exercised and its behaviour is unknown.

### The two config pairs are not interchangeable

- `0xBC28C4` / `0xBC28C5` (`start_lives`, `start_bombs`) — read by the continue
  path.
- `0xBC28EC` / `0xBC28ED` (`cfg_lives`, `cfg_bombs`) — read by the death path to
  refill bombs, read at stage init (`0x91BA`, `0x91C7`), and written by the result
  screen from the live counters.

Because the result screen writes the second pair back from the live counters, a
value set there can be overwritten when a stage ends. Both pairs were set to 8 and
still held 8 after several seconds of play.

## Where the game's objects live

Counters were reachable as globals. Enemies and shots are not: they live in
objects, so the first job is a map of the objects. This is what has been
established by reading the executable and a live process. None of it is needed
to read or write the counters.

### The stage object is `this`

The 38 KB update function at `0x2CCF0` takes its object in `rcx` and immediately
does `mov rdi, rcx`, then `movsxd rdx, [rcx+0x11608]`. That object is a static
buffer, not a heap allocation. The stage-start function at `0x36E30` sets it up:

```
0x36e42  lea   rsi, [rip + 0xb70577]   ; -> RVA 0xBA73C0
0x36e49  mov   rcx, rsi
0x36e4c  xor   edx, edx
0x36e4e  mov   r8d, 0x11628            ; size
0x36e54  call  0x2b8560                ; memset(obj, 0, 0x11628)
```

So the stage object is **RVA `0xBA73C0`, size `0x11628`** (71208 bytes). The
check is arithmetic: `0x36E6C` writes a dword to RVA `0xBB89C8`, and
`0xBA73C0 + 0x11608 = 0xBB89C8`, which is the state field the update function
dispatches on.

### How `this` is passed

The dispatcher at `0x36F4D` settles it:

```
0x36f44  mov   rax, [rbx + 0x10]        ; function pointer
0x36f48  test  rax, rax
0x36f4d  mov   rcx, [rbx + 0x38]        ; <-- argument
0x36f51  call  rax
```

The argument is the owner pointer stored at `+0x38` of the task node. This is why
`this` could not be found by looking for a call: it arrives through a task
dispatch, and the owner is stored in the node, not computed.

### Stage object layout

| Offset | Meaning |
|---|---|
| `+0x28` | most-used field in the update function (94 references) |
| `+0x9450`, `+0x9454` | counters immediately before the pool |
| `+0x94EA` | start of a 121-element pool, stride `0x110` |
| `+0x1157A` | end of the pool; start of the object's own tail |
| `+0x11608` | state index, `0..0x14`, dispatched through the table at `0x36414` |
| `+0x11610` | pointer to the player array; null outside a stage |
| `+0x11618`..`+0x11620` | word fields set alongside the state |

Two facts pin the pool's extent. `arrayrefs.py` shows intra-element offset `+0`
used for elements `0..121`, and the tail fields (`0x11600`, `0x11608`, `0x11610`,
`0x11618`…) used **only** at element 121. Since `0xBA73C0 + 0x11628` is the end of
the object, the pool is 121 elements and the fields from `0x1157A` on are the
object's own tail, not a 122nd element.

The pool is initialised uniformly, which is why it is not an entity pool: every
element reads `0d 00 3c 00` (word 13, word 60) for elements 0–101 and
`0d 00 00 00` for 102–120. Live entity data would not be uniform.

### The state table

`0x36414` holds 21 four-byte values used as absolute RVAs (`r15` is zero at
`0x2CD41`, so the table entries are not relative to the table). States 12–17 all
dispatch to `0x2FA4D`. This is what makes the state field identifiable: state 0
leads to `0x2CD69`, and `0x3083D` is the stage-teardown path that sets
`[rdi+0x11610]` to zero.

### Task nodes and their owners

The task system is a linked list of 0x40-byte nodes. The static sentinel is at
RVA `0x3EC5F0`; it has no function pointers, a self-pointer at `+0x30`, and links
at `+0x20` (prev) and `+0x28` (next). Nodes carry `+0` type (word), `+8`/`+0x10`/
`+0x18` function pointers, and `+0x38` the owner passed as `this`.

Walking only the sentinel's chain finds 10 nodes. Scanning `.data` for
self-referential structs finds more, because the nodes form **several** chains.
Seeding from every static node and following `+0x28` yields 25 nodes, types
`0..15`, owned by ten objects:

| Owner RVA | Node types | Notes |
|---|---|---|
| `0xBC2580` | 0, 14 | scene manager; its function `0x58330` calls the stage-start `0x36E30` |
| `0x3E02F0` | 1, 10, 15 | first fields are `1.0, 1.0` |
| `0x3ED010` | 2, 4 | holds the score and lives/bombs |
| `0x3EEF30` | 3, 4, 6 | |
| `0xBB89F0` | 5, 7 | functions in the `0x4Axxx` range, near the lives/bombs code |
| `0xA7E1E0` | 6, 9 | |
| `0xA4D030` | 8, 10 | |
| `0x955410` | 9, 11 | |
| `0x955348` | 11, 12 | |
| heap | 13, 15 | |

The renderer is at RVA `0x9552D0` (read by `0x37050` and by the HUD code). The
sprite blitter is `0x8480`; the sprite emitter `0x25B0` writes quad vertices into
the globals at `0x9553B0`…`0x9553F8`.

### The score/lives object

The object at RVA `0x3ED010` is a separate manager, not the stage object. Its
fields land exactly on the known globals:

| Field | Absolute | Known as |
|---|---|---|
| `+0x7AC` | `0x3ED7BC` | displayed score |
| `+0x17F4` | `0x3EE804` | lives |
| `+0x17F5` | `0x3EE805` | bombs |

Reading `+0x17F4` as a 16-bit value gives `2056` = `0x0808`, i.e. lives 8 and
bombs 8, which is how the identity was confirmed.

### Entity pools are still unidentified

This is the honest state. What a frame diff shows is that the heap holds
0x1C-byte records with three moving floats, grouped four per 0x70-byte block —
that is quad geometry, i.e. drawn output, not entity state. One owner,
`0xBB89F0`, has a 32-element array of 12-byte float triples at `+0x180`, but it is
zero while the game is paused, so it is unconfirmed.

**A frame diff is only meaningful while the game is running and unpaused.**
th06c pauses when it loses focus, and a paused game produced zero changed floats
in every object window tested, while an unpaused one produced 2899. Diffing a
paused game proves nothing.

## Tooling

Run everything with `python`, from inside `th06c-tools`.

| File | Purpose |
|---|---|
| `disasm.py` | PE reader + capstone disassembler, works from the EXE on disk |
| `gamestate.py` | named read/write of the confirmed fields |
| `keepalive.py` | background watcher that keeps lives and bombs topped up |
| `findrefs.py` | RIP-relative reference finder against a live process |
| `modbase.py` | module base address and section table |
| `memedit.py` | process open, read, write, region enumeration |
| `th06.py` | window and process discovery, screen capture |
| `hud.py` | counts HUD star icons by colour |
| `memscan.py` | general memory scanning |
| `findplayer.py` | read-only differencing scan; premise was wrong, see below |
| `hudwatch.py`, `bombcand.py` | earlier narrowing experiments |
| `offsets.py` | histograms the field offsets a function uses, revealing an object's layout |
| `arrayrefs.py` | finds every access to an inline array, folded modulo its stride |
| `tasklist.py` | walks the task list and prints each node's functions and owner |
| `findobj.py` | scans `.data` for objects matching a field signature |
| `framediff.py` | whole-memory frame diff filtered to moving coordinates |
| `poolscan.py` | frame diff, then infers each cluster's record stride |

`disasm.py` modes:

```
disasm.py 0x29990 0x80        # disassemble an RVA range
disasm.py --refs 0x3EE805     # find RIP-relative references to an RVA
disasm.py --ctx 0x4A6B2       # show the instruction at a displacement offset
disasm.py --func 0x3084D      # .pdata function boundaries
disasm.py --callers 0x297E6   # direct call sites
disasm.py --secs              # section table
```

`--ctx` exists because a reference is reported as the offset of the 32-bit
displacement field, not the start of its instruction. It tries each candidate
start and keeps the alignment where an instruction ends exactly at
`ref + 4`.

`--refs` verifies every candidate with capstone. See the pitfall below for why
that is necessary.

### Static analysis beats process scanning

Every address above was derived from the executable on disk, read-only, with no
game running. Code does not change between launches and only the base address
moves, so `disasm.py` needs no live process. Prefer this to scanning a running
game.

## Pitfalls

These cost most of the time in this investigation. They are worth reading before
touching the game again.

### A read-only process handle makes every write fail silently

`memedit.open_game()` defaults to `write=False`. `gamestate.py` originally called
it without `write=True`, so the handle lacked `PROCESS_VM_WRITE` and **every
write did nothing**. `write_mem` returned a bool that callers ignored, so the
failure was invisible.

The consequences were conclusions drawn from a no-op: the HUD star row appeared
"cached", the counter appeared "not stored as an integer", and a long hunt for a
separate player struct followed a phantom.

Both halves are now fixed: `gamestate.game()` opens with `write=True`, and
`write_mem` raises `OSError` on failure. **Verify a write actually landed before
drawing any conclusion from its absence.**

### Never hold a write on an unknown address

Two game crashes came from holding a probe value on batches of unverified
addresses. A held write is sustained corruption of whatever depends on that
address, not a read.

Writing the score and high score was safe because those addresses were already
confirmed. Spraying held writes over candidates that were merely *guessed* from a
disassembly listing is not, and it killed the process twice. Confirm an address
from code before writing to it.

### A paused game freezes the HUD, and the capture with it

`hud.count` reported a constant 3 stars while the display showed 8. Later
captures of the same state read 8 correctly, with runs at x = 2254, 2302, 2350,
2398, 2446, 2494, 2542, 2590.

The cause is the pause state. While the game is paused the HUD is not redrawn, so
no write to any counter changes the screen, and window captures keep returning the
last frame. Two signals confirm a paused game: `hud.scan` returns identical `raw`
bytes across calls, and the score in memory stops advancing.

Treat `hud.count` as a change *detector* rather than a precise counter, and
confirm a surprising reading with a second capture or by asking the user. In this
session the user's direct observation was correct and the instrument was wrong.

### `--refs` must not assume the displacement is the last field

`find_refs` originally computed the target as `disp_offset + 4 + disp`. That holds
only when the displacement ends the instruction. In `cmp byte [rip+disp], 8` and
`add dword [rip+disp], imm32`, an immediate follows the displacement, so the
formula returns a target that is too low by the immediate's size.

Both errors followed. The `cmp byte ptr [0x3EE805], 8` cap check at `0x2B60B` was
reported as a reference to `0x3EE804`, and three real references to `0x3ED7C0`
were reported against `0x3ED7BC`:

```
0x4c5ba  add dword ptr [rip + 0x3a11fc], 0x1f4   ; really targets 0x3ED7C0
```

`find_refs` now locates the displacement's true position by subtracting the size
of the instruction's immediate operands, and verifies each candidate by
disassembling the containing instruction. This removes invented references and
recovers missed ones: `0x2CC33`, inside the `mov word [0x3EE804], 0x302` reset,
only appeared after the fix.

The same formula is still used in `findrefs.py`, which works against a live
process. **Treat its output as unverified and confirm each hit with `disasm.py`.**

`--ctx` shares a limitation: it picks the alignment producing the most decodable
instructions, which is occasionally wrong. Cross-check against a `.pdata`
function start when the output looks implausible.

### `--func` boundaries are not always function entries

`.pdata` unwind regions can begin mid-function. At `0x296E9` the code reads flags
set at `0x296DD`, before the reported boundary. Disassemble across the boundary
before assuming a fresh function.

### The write-handle bug invalidated `findplayer.py`

`findplayer.py` snapshots addresses whose `+9`/`+0xA` bytes match the current
lives/bombs pair, then intersects after an in-game change. It returned zero
survivors because the premise in its original docstring — that the globals are
per-frame mirrors that overwrite writes — was false. The docstring now records
this. The differencing technique itself is sound.

## Environment

| Item | Value |
|---|---|
| Game executable | `C:\Program Files (x86)\Steam\steamapps\common\th06c\th06c.exe` |
| Executable size | 4,353,536 bytes |
| File image base | `0x140000000` |
| Runtime module base | `0x7ff711bd0000` (varies with ASLR) |
| `.text` | RVA `0x1000`, virtual size `0x2B9694` |
| `.data` | RVA `0x324000`, virtual size `0x8A24D4` |
| `.pdata` | RVA `0xBC7000`, 4,514 function entries |
| Python | 3.14 |
| capstone | 5.0.9 (installed for this work) |

Sections are listed by `disasm.py --secs`.

## Open items

- Whether the hardcoded `0x302` override at `0x2CC30` fires on a game over was not
  tested; both of its trigger flags were false on the observed continue.
- A death was observed to decrement lives by one and refill bombs from
  `cfg_bombs`. Whether the bomb refill survives a game over, and whether
  `cfg_bombs` is overwritten by the result screen before the next death, were not
  tested.
- The HUD star rows were confirmed to follow the globals while the game is
  running. The displayed star count was never confirmed against a fresh capture
  for lives, only for bombs.
- Lives and bombs above 8 were not tested. `gamestate.py` caps them at 8.
- The `+0x11610` object passed to the 38 KB update function at `0x2CCF0` is now
  identified: RVA `0xBA73C0`, a static 0x11628-byte stage object, passed as `this`
  through the task node's `+0x38` owner field. See "Where the game's objects
  live".
- The entity pools for enemies and shots are not yet identified. A frame diff can
  only distinguish them while the game is unpaused; th06c pauses when it loses
  focus.
- `--ctx` picks an alignment by decodability and is occasionally wrong.
- `findrefs.py` took an absolute address only, so passing the RVA that `disasm.py`
  prints silently matched nothing. It now accepts either. Its formula matches
  `disasm.py`, verified against `0x3EC5F0` (26 references, same as a manual scan).
- `hud.py` counts icons by colour in a fixed screen region
  (`DEFAULT_REGION = (2205, 350, 700, 145)`); it will need adjusting for a
  different resolution.

## The game was paused in its menu, so early diffs measured the menu

A capture of the game window (`peek.py`) showed the pause overlay, not gameplay:
`一時停止` / `一時停止解除` / `タイトルへ戻る`, with the cursor on
`一時停止解除` (Resume). th06c pauses when it loses focus, and it stays paused
until the menu entry is confirmed -- refocusing the window alone does not resume
play.

That invalidates a whole class of measurements. Several diffs in this session
compared memory before and after synthetic key input and reported a stride-0x100
pool at `0x13045600000` with fields at `+0x6C` and `+0x9C` that moved out and
back by `±151.43` and `±101.01`. Those are pause-menu item transforms, not the
player. They reverse cleanly under an out-and-back key press because the menu
cursor moves and returns, which is exactly the signature the differential was
designed to find.

Consequences worth keeping:

- A differential scan needs proof that the game is in the state under test.
  Capture the window and look before trusting a diff. `peek.py` exists for this.
- The `0x100`-stride records at `0x13045600000` are transforms, not entities.
  Each record holds two projection-style matrices and a view matrix with a
  rotation (`0.9807` / `0.195519`) and a translation column at `+0x9C` / `+0xAC`.
  Treat that buffer as camera or UI state.
- th06c renders through 3D transforms, so an entity's screen position is derived,
  not stored as a plain 2D float pair. A (x, y) pair scan over the playfield
  range returns quad vertices and matrix rows, not entity positions.

### Screen-space extraction as the direct route

Because the window can be captured and read back, bullet and enemy positions can
be taken from the framebuffer at 60 fps by tracking sprite centroids, without
decoding any entity structure. That is a much shorter path to "read enemy and
shot locations" than identifying the pools, and it survives patches. The
trade-off is precision and identity: a centroid gives an approximate position
and no stable object id across frames, whereas pool access gives exact
coordinates and per-object fields.

Tools added this session: `pairscan.py` (coordinate-pair and stride scan),
`playermove.py` (drive one direction and diff), `playerdiff.py` (out-and-back
differential), `peek.py` (capture the window to an inspectable image).

### Pool hunt with the game live

With the game confirmed unpaused (`peek.py` showed the stage, not the menu), two
scans ran.

**Player movement drives the vertex layer, not a sim struct.** `playerdiff.py`
(hold Right 700 ms, then Left 700 ms, keep only values that reverse) returned
3831 movers, essentially all inside `0x130437f0000`. Dumping the region shows
0x30-byte records of two 0x18-byte vertices, each `(0, nan, u, v, x, y)`:

```
+000   0  nan  0.3125  0.238281 | 217  -222.841  0  nan  0.285156  0.269531 | 231  -222.841
```

The two x values differ by 14, the sprite's width, and the UV pairs differ too.
So this is the drawn sprite/vertex layer. Its y values sit near `-224`, i.e. a
space centred on the playfield rather than 0-based.

**With the player still, that buffer stops moving.** `hunt.py` (two snapshots
0.3 s apart, player stationary) does not list `0x130437f0000` at all. The busiest
movers are instead:

| Bucket | Movers | Stride | Values |
|---|---|---|---|
| `0x1307c700000` | 1856 | 0x8 | ±0.25 |
| `0x13076010000` | 892 | 0x8 | ±0.25 |
| `0x1307c710000` | 862 | 0x8 | ±0.25 |
| `0x1307c1a0000` | 830 | 0x8 | ±0.25 |
| `0x13045600000` | 223 | 0x6C/0x70 | camera matrices |
| `0x13046aa0000` | 64 | 0x48 | profiler pool |

The four stride-0x8 clusters with values inside ±0.25 are audio or DSP buffers,
not entities.

**What this means.** Bullets are visibly moving on screen, yet no bucket shows a
population of moving playfield-range coordinate pairs. Combined with the 3D
transform pipeline, the likeliest explanation is that bullet positions are not
stored as absolute 2D floats in a form these filters accept -- they may be
relative to a parent transform, or outside the assumed value range. Motion
diffing has found the graphics layer twice and the sim layer not at all.

**Next step that does not depend on motion.** Read a coordinate off the screen,
convert it with the playfield mapping (about 3.0 px per playfield unit at
3440x1440), and search memory for that value. A value search works on static
data, so it does not require the object to be moving between snapshots.

### Correction: the "no pools" result measured a paused game

The entry above is wrong in its conclusion, and the mistake is worth recording.

The game **auto-pauses and shows its pause menu whenever it loses focus**, and
the terminal holds focus for every one of these tools. `peek.py` had caught the
game unpaused, which made the game look live, but that frame was captured in the
moment just after it was resumed.

`pixdiff.py` settled it: four frames 0.3 s apart, **0 of 1440 rows differ**. A
frozen screen. A fresh `peek.py` then showed the pause menu itself --
一時停止 / 一時停止解除 / タイトルへ戻る -- sitting over the playfield.

So `hunt.py`'s 464 changed pages were audio, DSP and profiler buffers, which keep
updating while paused, and its 4824 "movers" were the stride-0x8 ±0.25 audio
clusters. The player-driven vertex activity that `playerdiff.py` found came from
`SendInput`, which necessarily focused the window and resumed the game. The
scans did not disagree about the game; they disagreed about whether it was
running.

Two consequences:

- `hunt.py`'s default `--vmin 0.01` admits audio samples, which then dominate the
  cluster ranking and bury any coordinate pool. A floor of 0.5 excludes them.
- Any differential scan must establish liveness first. `livescan.py` does that:
  it polls with `PrintWindow` (which reads the frame without stealing focus),
  starts the scan the moment two frames differ, and re-checks liveness afterwards
  so a game that paused mid-scan is reported rather than silently trusted.

### A valid live scan, and the bullet render pool

`livescan.py` fixed the measurement problem: it waited 58.1 s for the game to go
live, confirmed 676 differing rows, scanned, then re-checked liveness and
reported the game still live. That is the first trustworthy differential scan in
this project.

It found real pools -- stride-0x30 arrays with hundreds of consecutive records:

| Bucket | Movers | Dominant gap |
|---|---|---|
| `0x13045ba0000` | 2362 | 0x18/0x14/0x1c |
| `0x1304a600000` | 798 | 0x30 x770 |
| `0x130466e0000` | 470 | 0x30 x385 |

Dumping `0x1304a600000` gives a 12-float record:

```
x=130.561  y=-92.8203  z=0  0  0  -1  0x00FFFFFF  0  u=0.128906  v=0.941406  0  0
                                 ^^^ white colour    ^^^^^^^^^  ^^^^^^^^^  texel coords
```

Records come in runs of 6 sharing one position, and the six UV pairs trace two
triangles of one quad: `(33,241) (47,241) (33,255) | (47,241) (33,255) (47,255)`.
Consecutive quads use adjacent 14-texel sprite cells, so different bullets carry
different sprites. This is **the bullet render pool**: 6 vertices per bullet,
0x120 bytes each, positions in playfield-centred coordinates.

Rendering those positions as a playfield map reproduced the on-screen layout --
a blob at x 160-360, y 64-256, exactly where the bullets were during the
彩符「彩雨」 spell card.

Two things this settles:

- **The address is not stable.** Five minutes later the game was on a different
  stage with an empty playfield, and `0x1304a600000` held unrelated data with no
  6-vertex runs at all. The pool has to be located per session, not hardcoded.
- **The sim/render distinction is about ownership, not values.** The sim computes
  a bullet's position and the renderer copies it into this pool each frame. A
  position read here *is* the game-logic position, one frame behind. For a bot
  the two are equivalent.

**Freezing works.** `freezeread.py` sends Esc, which opens the pause menu with
the cursor already on Resume, giving a frozen game for timing-free reads (0
differing rows afterwards). That matters because a live bullet moves several
pixels per frame, so a memory search for its position misses by the time the scan
arrives. It deliberately does not resume -- Z on the wrong menu item would
discard the run, so that stays a human decision.

The search itself came back empty, but only because it ran on an empty playfield:
there were no bullets to find. It needs a live game with bullets on screen, which
means locating the pool live first and freezing afterwards.

### Framebuffer route: simplified screen and blob extraction

`simplify.py` downscales the captured window (read-only, `PrintWindow`, no focus
stolen) and `blobs.py` crops the playfield, thresholds bright saturated pixels,
takes connected components, and draws the blobs. Both work while the game is
paused, which is the point: no resume cycle is needed to read positions.

Window geometry: the game frame is 640x480 scaled 3x and centred in the
3440x1440 window, so the frame spans x 760..2680 and the playfield (384x448 at
(32,16)) is the box (856,48)-(2008,1392). `blobs.py` works on that crop at
1/6 scale (192x224).

A first pass with `--lum 140 --sat 40` found 47 blobs and missed dim bullets; at
`--lum 115 --sat 35 --minarea 3` it finds 112, which tracks the on-screen bullet
layout but also admits the score bar and pause text. `blobs.py.cmp.bmp` puts the
crop next to the detected blobs for comparison.

The paused-state memory pool is not cooperating: anchoring on the vertex
signature (`-1.0f` normal at record+0x14, `0x00FFFFFF` colour at +0x18) finds
only ~55 records in the whole 415 MiB, and the vertex array in
`0x1304a640000` (stride 0x30, vertices at addresses ≡ 0 mod 0x30, region base
≡ 0x10) has valid positions but no reliable six-vertex-per-quad grouping in this
frame. The framebuffer route needs no such decoding, so it is the shorter path.

### Two-layer map: bullets and light beams

A single threshold missed the light beams, because they are dimmer than the
bullet sprites but much larger. `screenmap.py` fixes that by splitting the bright
mask by connected-component area: components under `--big` become dots (bullets),
components at or above it are drawn as the filled shape they occupy (beams, the
enemy, the score bar).

At `--lum 55 --sat 20 --big 60` on the paused 彩雨 frame it finds 81 small and 15
large components. The large ones land where the beams are:

| Playfield box (x0,y0)-(x1,y1) | Area | What |
|---|---|---|
| 104,84 - 280,208 | 2630 | enemy + wings |
| 152,252 - 194,446 | 980 | vertical light beam |
| 170,22 - 226,88 | 575 | enemy body |
| 284,118 - 382,150 | 409 | right curved beam |
| 0,84 - 80,108 | 292 | left beam |
| 64,2 - 332,20 | 377 | score bar |

`map.bmp.cmp.bmp` is screen | map. The beams and enemy now appear; the bullets
remain separate dots. The threshold is the whole game here: at `--lum 100` the
beams vanish, at `--lum 45` the background gradient starts filling in.

### The paused frame cannot validate the pool

Tried to read the bullet pool while the game was paused with bullets on screen,
using the framebuffer as ground truth. It does not line up, and the reason looks
structural rather than a bug in the extraction.

Method: detect bright small blobs in the playfield crop, convert their centres to
playfield units, then search writable memory for the matching float pair with a
tolerance. The search itself is sound -- pointed at the known vertex position
(179.719, -163.387) it finds `0x1304a644090`, `+0xc0`, `+0xf0` exactly. Pointed
at the visible bullets it finds **nothing**, for either the centred or 0-based
convention.

Meanwhile the same region does contain 250 stride-0x30 vertices with the
`[x,y,z,0][0,-1,color,0][u,v,0,0]` layout, and drawing their positions over the
screen overlaps some bullets but not the vertical bullet line and not the lower
half. So the array is real but is not the visible bullet set.

Working hypothesis: the vertex pool is rebuilt every frame, and while paused the
frame being built is the pause overlay, not the playfield. The bullets on screen
are the last gameplay frame retained in the framebuffer, which is why a
framebuffer read sees them and a memory read does not.

If that holds, the pool can only be read from a live game -- which is exactly
what `livescan.py` was for. Its one good run found the pool (798 movers at stride
0x30), so the route works; the paused shortcut does not.

Also noted: region `0x1304a640000` (0x30000 bytes) is referenced by 226 pointers
from two arrays around `0x13046179ff0` and `0x130461df0b0`, spaced 0x2a0 apart,
each pointing at a 0x100-aligned address inside the region. That is a table of
something (resource or object records), and walking it back to a static address
is the likely way to locate the pool without a scan.

### The pool is real: stride-0x18 sprite quads

The live run (`liverun.py`) answered it. With the game actually running it found
20,814 moving floats, and the biggest cluster is not the stride-0x30 array the
paused analysis kept turning up. It is a **stride-0x18 vertex array in groups of
six**, which is one sprite drawn as a quad:

    +0x00  x      +0x04  y      +0x08  z      +0x0C  (unused)
    +0x10  u      +0x14  v
    +0x18  vertex 1 ...                       six vertices per sprite,
    +0x60  vertex 4 == vertex 2               0x90 bytes per sprite
    +0x78  vertex 5 == vertex 1

Vertex 4 repeats vertex 2 and vertex 5 repeats vertex 1, i.e. the quad is
expanded to two triangles as (0,1,2) and (2,1,3). The four distinct corners are
about 16 units apart, matching the 14-16 unit bullet sprite.

**Coordinates.** Reading the UVs settles the axes: u rises with x and v rises as
y falls, so x is right-positive and y is up-positive. Mapping to the playfield
panel is therefore a half-scale with a y flip:

    panel_x = 0.5 * x        panel_y = -0.5 * y + 12

At that transform the extracted centres sit on the bullets -- `livefit.bmp`
panel 2 and the 3x zoom in `zoom.bmp`. The residual offset is a few pixels and is
consistent with the frame and the snapshot being a frame or two apart while the
knives move.

**Why it is still fragile.** Over one session the array moved through
`0x13045b40000`, `0x1304a690000`, `0x1304a8c0000`, `0x13043670000`; several
coexist, and the coordinate origin differs between them (one fit wanted
`bx=-84`, another `bx=0`). It is a generic sprite buffer, so it also holds the
player, effects and HUD. And it is only meaningful while the game is live --
paused, the same buffer is rebuilt with the pause overlay, which is what made
the paused reads look wrong all along.

So the pool is reachable, but locating it needs the live differential scan each
stage. The framebuffer route needs none of that.

## Hit detection found (static, no live process needed)

The game has a proper collision routine and it was reachable without any of the
pixel or pool work. Two things made it quick: `disasm.py` already had function
boundaries from `.pdata`, and a public decompilation of the 1.02h original
(`github.com/GensokyoClub/th06`) gives the struct layouts and the call graph. The
port is 64-bit, so struct sizes differ, but field names and code shape carry over.

New tool: `codeindex.py`. It disassembles every `.pdata` function once and pickles
three tables -- RIP-relative refs by target, memory-operand displacements by value,
and direct call targets -- then answers `--ref`, `--disp`, `--calls` instantly.
Building it took one pass; every query below is a lookup.

### The collision test: `Player::CheckGraze`, RVA `0x4C3E0`

Found by searching `.text` for the two graze caps, **9999** and **999999**. Both
appear at only two sites, and the code at `0x4C521` is unmistakably
`Player::ScoreGraze` inlined:

```
0x4c521  cmp  byte [rip+0xb75c99], bl   ; -> 0xBC21C0  g_Player.bombInfo.isInUse
0x4c529  mov  eax, [rip+0x3a22b1]       ; -> 0x3EE7E0  grazeInStage
0x4c52f  cmp  eax, 0x270f               ; 9999
0x4c53e  mov  eax, [rip+0x3a22a0]       ; -> 0x3EE7E4  grazeInTotal
0x4c544  cmp  eax, 0xf423f              ; 999999
```

The enclosing function starts at `0x4C3E0` and its head is the decomp's
`CheckGraze` geometry, instruction for instruction:

```
0x4c406  movss xmm6, [rip+0x2c1bba]     ; -> 0x30DFC8  0.5f
0x4c3eb  movss xmm2, [rip+0x2c1ce9]     ; -> 0x30E0DC  20.0f
0x4c434  mulss xmm1, xmm6               ; size.x * 0.5
0x4c441  subss xmm9, xmm1               ; center.x - size.x/2
0x4c45f  subss xmm9, xmm2               ;                 - 20.0
0x4c446  lea   rax, [rip+0xb7381b]      ; -> 0xBBFC68  this->bombProjectiles
```

So it is an **AABB test, not distance math** -- which is why the float-op-density
ranking never pointed at it. The routine expands the bullet box by 20 units, tests
it against each bomb projectile (return 2 = destroy bullet), then against the
player's own hitbox (return 1 = graze, which scores and is where the 9999 caps
live), else 0.

### The caller: `BulletManager_OnUpdate`, RVA `0x0DB90`

`CheckGraze` has exactly one caller. That function is the bullet loop:

```
0x0db90  push rbx / r14 / r15 ; sub rsp, 0x100
0x0dba4  lea  rbx, [rcx + 8]            ; rbx = mgr + 8 = bullets[0]
0x0dba8  mov  r14, rcx                  ; r14 = mgr
0x0dcaf  mov  [r14 + 0xe8808], r12d     ; mgr->bulletCount = 0
0x0dcc0  cmp  word [rbx + 0x44], r12w   ; curBullet->state
0xe3fb   cmp  byte [rbx + 0x5c8], r12b  ; curBullet->isGrazed
0xe40b   lea  rdx, [rbx + 0x30]         ; &curBullet->pos
0xe404   lea  r8,  [rbx + 0x5a4]        ; &curBullet->sprites.grazeSize
0xe40f   call 0x4c3e0                   ; Player::CheckGraze
0xe4da  add  rbx, 0x5d0                 ; stride
0xe4fb  cmp  ebp, 0x280                 ; 640 bullets
0xe501  jl   0xdcc0
0xe50d  lea  rbx, [r14 + 0xe8818]       ; then lasers[0]
```

`.pdata` labels this function `0xDBE6..0xE570`, which is wrong -- the real entry is
`0xDB90` and `0xDBE6` is inside it. Worth remembering: `.pdata` boundaries are a
good index but not gospel.

### The bullet array: `g_BulletManager` = RVA `0x955410`

Found from the other end. `BulletManager_RegisterChain` takes the bullet sprite
path, and `data/etama.anm` (敵弾) has exactly one reference, in the stage-init
function at `0x24F10`. That neighbourhood names everything:

```
0x24edd  lea  rbx, [rip+0x93052c]       ; -> 0x955410  g_BulletManager
0x24ee9  mov  r8d, 0xe8810
0x24eef  call 0x2b8560                  ; memset(mgr, 0, 0xE8810)
0x24f10  lea  rax, [rip+0x2e7431]       ; -> 0x30C348  "data/etama.anm"
0x24f17  mov  [rip+0xa18d02], rax       ; -> 0xA3DC20  mgr->bulletAnmPath
0x24f25  lea  rax, [rip-0x1739c]        ; -> 0x0DB90   update fn
0x24f33  mov  [rip+0x3c75fe], rax       ; -> 0x3EC538  chain node
```

Layout, and every line of it cross-checks against an independent observation:

| Offset | Field | Absolute RVA |
|---|---|---|
| `+0x008` | `bullets[0]` | `0x955418` |
| `+0xE8808` | `bulletCount` | `0xA3DC18` |
| `+0xE8810` | `bulletAnmPath` | `0xA3DC20` |
| `+0xE8818` | `lasers[0]` | `0xA3DC28` |

`bullets[0] + 640 * 0x5D0 = 0xA3DC18`, which is exactly where the loop says
`bulletCount` lives. The first memset ends at `0xA3DC20`, exactly where
`bulletAnmPath` is written. The second memset targets `0xA3DC28`, exactly
`lasers[0]`. `0x955410` also already appeared in the chain-owner table.

### Bullet fields (port layout, differs from the 32-bit original)

| Offset | Field |
|---|---|
| `+0x014` | `exFlags` (u16) -- mask `0xDC0` = the out-of-bounds angle/bounce flags |
| `+0x030` | `pos` (center; the `&pos` passed to `CheckGraze`) |
| `+0x044` | `state` (u16) -- `0` inactive, `1` fired, `2..4` spawning, `5` despawning |
| `+0x050` | `spriteBullet`, then 4 more `AnmVm` at stride `0x110` |
| `+0x5A4` | `sprites.grazeSize` |
| `+0x5B4` | `outOfBoundsTime` (u16) |
| `+0x5C8` | `isGrazed` (u8) |

Size `0x5D0`. The original has `pos` after the sprites at `+0x560`; the port moved
it to `+0x30`, so offsets from the decomp must be re-derived, not copied.

### Related addresses

| Name | RVA |
|---|---|
| `g_BulletManager` | `0x955410` |
| `Player::CheckGraze` | `0x4C3E0` |
| `BulletManager_OnUpdate` | `0x0DB90` |
| `g_Player.bombProjectiles` | `0xBBFC68` |
| `grazeInStage` | `0x3EE7E0` |
| `grazeInTotal` | `0x3EE7E4` |
| `g_GameManager` | `0x3ED010` (size `0x1A80`; `0x3ED010+0x1A80 = 0x3EEA90`) |

Not yet confirmed against a live process -- the game had exited by the time the
mapping was complete. The check is cheap and needs no gameplay: `bulletAnmPath` at
`mgr+0xE8810` must read back as a pointer to `"data/etama.anm"`.

## Enemies are fatal too, via a different function

`CheckGraze` is only graze/first-contact. The actual **death** test is
`Player::CalcKillBoxCollision`, and it is what calls `Player::Die()`.

### `Player::CalcKillBoxCollision`, RVA `0x4C690`

Sits immediately after `CheckGraze` in `.text`, which is how it was found. The
head is the decomp verbatim -- `size / 2.0f`, an AABB, then a loop over
`bombProjectiles` at stride `0x10`:

```
0x4c6d1  mulss xmm1, xmm3            ; size.x * 0.5
0x4c6e2  subss xmm5, xmm1            ; center.x - size.x/2
0x4c6e6  addss xmm7, xmm1            ; center.x + size.x/2
0x4c6f6  movss xmm1, [rax]           ; bombProjectiles[i].size.x
0x4c6fa  ucomiss xmm1, xmm8          ; == 0.0f -> skip
0x4c749  add   rax, 0x10             ; PlayerRect stride
0x4c74d  cmp   ecx, 0x10             ; 16 bomb projectiles
0x4c752  movss xmm0, [rsi + 0x410]   ; this->hitboxTopLeft.x
0x4c774  comiss xmm5, [rsi + 0x41c]  ; this->hitboxBottomRight.x
0x4c78e  cmp   byte [rsi + 0x73b8], dil  ; this->playerState
```

Player fields: `hitboxTopLeft` `+0x410`, `hitboxBottomRight` `+0x41C`,
`playerState` `+0x73B8` (byte), `bombProjectiles` `0xBBFC68`.
`PLAYER_BOMB_PROJECTILE_COUNT` is 16 in this build.

Return values: **2** = a bomb projectile covers it, **1** = hit (and `Die()` is
called when alive), **0** = miss.

### Callers

`CalcKillBoxCollision` has exactly two callers:

| Caller | RVA | What it tests |
|---|---|---|
| bullet loop | `0x0DB90` | bullets already flagged `isGrazed` |
| enemy loop | `0x20780` | every collidable enemy |

`CheckGraze` has one caller (`0x0DB90`), so bullets are the only thing that
grazes; enemies can only kill.

### The enemy loop: RVA `0x20780`

`.pdata` again labels it `0x2078C`; the real entry is `0x20780`
(`mov rax, rsp / push r14 / sub rsp, 0xf0`). Same 12-byte `.pdata` drift as the
bullet loop.

```
0x207e0  lea rbx, [r14 + 8]          ; enemies[0] = mgr + 8
0x2083f  mov [r14], r12d             ; mgr->enemyCount = 0
0x20850  cmp byte [rbx + 0xb4], r12b ; isSlotOccupied (bit 7) -> skip if clear
0x2085d  inc dword [r14]             ; mgr->enemyCount++
0x20860  lea rbp, [rbx + 0xa8]       ; &curEnemy->position
0x20dfd  call 0x4c690                ; Player::CalcKillBoxCollision
0x20e07  movzx eax, byte [rbx + 0xb5]
0x20e0e  and   al, 9                 ; isInteractable | isBoss
0x20e10  cmp   al, 1                 ; interactable AND not boss
0x20e14  add   dword [rbx + 0x21c], -0xa   ; curEnemy->life -= 10
0x21357  add   rbx, 0xfd0            ; stride
0x2136c  jl    0x20850
```

### `g_EnemyManager` = RVA `0xA7E1E0`

Not a stored chain callback -- a scan for a pointer to `0x20780` found exactly one
hit, in chain node `0x3EC6F0` (type 9), whose `+0x38` owner is `0xA7E1E0`. That
owner was already in the chain-owner table from the earlier chain work.

| Offset | Field |
|---|---|
| `+0x000` | `enemyCount` |
| `+0x008` | `enemies[0]`, **stride `0xFD0`**, 257 slots |
| `+0x0A8` | `position` |
| `+0x0B4` | flags byte 1 -- `isSlotOccupied` is bit 7 |
| `+0x0B5` | flags byte 2 -- `isInteractable` b0, `isCollidable` b1, `hasBeenInBounds` b2, `isBoss` b3, `isDamageable` b4 |
| `+0x0B6` | flags byte 3 -- `isInvisible` b3 |
| `+0x21C` | `life` |
| `+0x2A4` | `hitboxDimensions` |

Note `hitboxDimensions / 1.5f` is used for the touch test but the raw
`hitboxDimensions` is used for player-shot damage.

### The rule for "will this enemy kill me if I touch it"

```c
isCollidable && isInteractable && !isBoss && hasBeenInBounds
```

Confirmed live: a midboss at slot 0 read `+0xB5 = 0x2D` (isBoss set) and is not
fatal; the 17 fairy slots read `+0xB5 = 0x17` and are. And `enemyCount = 18`
matched the count of slots with bit 7 of `+0xB4` set, independently.

### Verified live

| Check | Result |
|---|---|
| `bulletAnmPath` reads back `"data/etama.anm"` | pass |
| `enemyCount` == slots with `isSlotOccupied` | 18 == 18 |
| boss flagged non-fatal, fairies flagged fatal | pass |

## entities.py -- everything fatal, read from game state, overlaid on screen

`entities.py` reads both fatal sources plus the player and draws them on the
playfield. It supersedes `bullets.py`, which only knew about bullets.

### g_Player = RVA 0xBB89F0

Found from the `lea rcx` feeding `Player::CalcKillBoxCollision` at `0xE489`:

```
0x00e489  lea rcx, [rip + 0xbaa560]   ; -> RVA 00bb89f0
0x00e490  call 0x4c690                ; Player::CalcKillBoxCollision
```

| Offset | Field |
|---|---|
| `+0x410` | `hitboxTopLeft` |
| `+0x41C` | `hitboxBottomRight` |
| `+0x73B8` | `playerState` (byte, 0 = alive) |

The hitbox is **2.5 x 2.5** units -- th06's tiny player hitbox, not the sprite.

### Coordinates: y-down, origin (856,48), 3 px per unit

The window is 3440x1440. The game's 640x480 frame is scaled 3x and centred, so
it occupies x = 760..2680. The playfield sits at native (32,16)-(416,464), which
maps to window (856,48)-(2008,1392) -- 1152x1344 px for 384x448 units.

The y direction was **measured, not assumed**. For each on-field enemy, the pixel
at the predicted position was scored by how far it deviates from its local
background, and both conventions were tried:

| Convention | Mean deviation over the same 13 enemies |
|---|---|
| **y-down** (`py = y * 3 / 6`) | **28.9** |
| y-up (`py = (448 - y) * 3 / 6`) | 6.6 |

Every enemy but one matched y-down; the single y-up "win" was the pause-menu text
at that spot. Independently, the player's hitbox centre reads (192.0, 384.0),
which is overlay pixel (96,192), and that pixel sits in the middle of a
structured 12x16 blob in the lower middle of the field while control probes
elsewhere are flat background. Two unrelated structs agreeing on the same
convention is what makes this settled.

### The fatal-on-touch rule, confirmed live

```
isSlotOccupied && isInteractable && isCollidable && hasBeenInBounds
    && !isBoss && !isInvisible
```

A paused stage with 18 enemies: the midboss in slot 0 read `+0xB5 = 0x2D`
(`isBoss` set) and is not fatal; the 17 fairies read `+0xB5 = 0x17` and are.
`enemyCount` also equals the number of slots with bit 7 of `+0xB4` set, which
nothing forced to agree.

### Gotcha: the row data is BGR

`th06.capture` returns 24bpp **BGR**, so the overlay colour literals have to be
written B, G, R. Writing them in RGB order silently swaps red and cyan -- the
first render drew fatal enemies in the harmless colour and looked plausible.
`pngout.write_png` converts to RGB on the way out, so a PNG shows the true
colour even when the BMP convention is confusing.

### Also added

`pngout.py` -- a minimal PNG writer (zlib + IHDR/IDAT/IEND). Pillow is not
installed, and the read tool would not accept the larger BMPs.

### All three verified live, against sprites

`entities.py --catch N` polls `bulletCount` at 50Hz and only captures once
bullets are actually in flight, because a paused game or a dialogue scene has
`bulletCount == 0` and there is nothing to align against.

| Source | Marker | Verified how |
|---|---|---|
| bullets | green | dots sit on the midboss's ring-bullet column |
| enemies | red / cyan | dots sit on the fairy sprites; cyan on the non-fatal midboss |
| player | white | dot sits on the player sprite at the hitbox centre |

**Bullet array self-check.** Across a live wave, `bulletCount` equalled the number
of slots with `state != 0` at every sample:

```
bulletCount = 20  20  40  40  60  60  70  70
```

Nothing forces those to agree, so this independently validates the array base,
the `0x5D0` stride, and the `state` offset at `+0x044`.

### A false alarm worth recording

An earlier frame appeared to show bullets on screen while `bulletCount` read 0.
Those red squares were **items** (EoSD power items are red squares with a white
border), not bullets -- confirmed by a later frame where the real bullets, red
rings, are drawn alongside them and only the rings pick up green markers. The
`bulletCount = 0` reading was correct; the guess about what was on screen was
wrong. Cheap lesson: identify the sprite before concluding an address is broken.

### Player state matters for "can I be hit right now"

`Player::CalcKillBoxCollision` returns 1 **without** calling `Die()` whenever
`playerState != PLAYER_STATE_ALIVE`, so the lethal window is narrower than the
hitbox test:

```
0 ALIVE            can die
1 SPAWNING         cannot die
2 DEAD             already dead
3 INVULNERABLE     cannot die
```

A live frame during a respawn read `playerState = 3` while 70 bullets were in
flight. Any "am I about to die" check has to include this, or it will report
deaths that cannot happen.

---

## The decision engine, and what it cannot do yet

The idea was to let a decision model play: render the game state, ask which way
to move, hold that arrow key. The pieces exist now. What follows is what was
measured, and the two mistakes that had to be corrected before the numbers meant
anything.

### Reading the model matters more than the encoding

The DE has two readouts. DE-1 writes the question once; **DE-2 writes it twice**,
separated by a fixed sentence. We were reading a DE-2 model with the DE-1
prompt, and on the eight hand-built scenarios that cost three of eight -- the
player-centred grid went from 5/8 to 8/8 once the repeat was added. The readout
contract is not a detail.

### Latency: the honest number is ~130-300 ms

Template path, one token, uncontended, median of 7:

| evidence | size | round trip |
| --- | --- | --- |
| 32x36 character grid | 1696 chars | 242 ms |
| 15x15 window on the player | 592 chars | 149 ms |
| per-direction table | 516 chars | 128 ms |

Building the text costs 0.02-0.28 ms, so the round trip is the whole budget. At
60 fps that is 8-15 game frames per decision.

Two things that *look* like latency but are not. Rendering a picture is pure
Python and costs **272 ms at 768x896** -- that is a real cost, and it is ours,
not the model's. And an early image measurement came back at 9.3 s, which turned
out to be self-inflicted: the prompt demanded a chain of thought. The SDK's
image read is a single direct read with no thinking, and comes back in ~194 ms.
Asked to answer briefly, the model used 3 output tokens even with a 2000-token
budget.

### The evaluation problem, which is the real one

Every result here rests on 8 hand-built scenarios plus 12 generated ones. Both
were misleading in opposite directions.

On loose scenarios -- most moves safe -- the model scores at or below chance,
but chance is 67%, so the test proves nothing. On *tight* frames, 1-3 of 9 moves
safe, it looked much better: 7/12 against a random baseline of 3.7/12. Then the
baseline that mattered: **a constant "always move up" policy scores 9/12 on
that set**, because the safe sets were skewed toward up. The model was worse
than a policy that ignores the board.

So: no configuration has yet been shown to read the state. Every apparent win
has dissolved into test design -- first small samples, then an uncontrolled
positional prior. Any future scoring needs constant-policy baselines computed
per scenario set, not just a random one.

### What is wired up

  `state.py`       one bulk read per frame. The bullet array and enemy array are
                   930 KB and 1016 KB, so each is a single ReadProcessMemory;
                   entities.py's per-field reads would be ~2500 calls a frame.
                   Velocities are differenced across frames by slot index.

  `de_client.py`   the DE-2 readout as specified, text and image.

  `controller.py`  60 Hz sampler plus a decision worker. The sampler must never
                   block or the player stops moving, so decisions happen on the
                   worker and the direction is held until the next one lands.

  `oracle.py`      scores a decision after the fact by simulating which moves
                   survive. It never goes into a prompt.

The controller logs every decision to JSONL including the oracle's safe set, so
a run can be scored afterwards without reproducing it.

### Unknowns that will bite

**The player's speed was a placeholder, and turned out to be right.** The view
tells the model 4.0 units per frame. `state.py --calibrate-speed` measured a
median step of exactly 4.00 u/f in each of right, up and down, so the guess was
correct and no change was needed. The `dist/frames` column reads 3.91-4.07 only
because the frame count is derived from wall time and rounds. One caveat for the
tool itself: the `left` reading came back at 1.46 u/f with a median step of 0.00
because the player was already against the left wall. A blocked direction is not
a slow one, and the tool does not currently tell them apart.

**Bullet velocity has no known offset.** Differencing works but a bullet that
just spawned has no history for a few frames, and accelerating or curving shots
are extrapolated as straight lines.

**The bullet kill radius is a guess** (4.0 units). The graze size in the struct
runs 8-64 and is not the kill hitbox.

None of these three affect whether the loop runs. All three affect whether the
oracle's opinion of a decision is trustworthy.

### Bullets that cannot kill were being drawn and collided with

The largest single defect found so far, and it was in both the evidence and the
critic at once. `state.py` has always computed `live = state == 1` per bullet,
and **nothing consumed it**. The view drew every allocated bullet as a threat and
the oracle collided against every one, so on 43% of frames in a 500-decision run
the model was shown bullets that could not hurt it -- 136 of them on one frame.

That was not a guess about the state enum. `BulletManager_OnUpdate` dispatches on
state at `0xdcc0` (`r15 == 1`), and the path for states 2-4 is:

```
0xddd3  call 0x62c0                 ; spawn animation update
0xddd8  test eax, eax
0xddda  je   0xe4c5                 ; still spawning -> next slot, NO graze test
0xddee  mov word [rbx + 0x44], r15w ; animation done -> state = 1
```

While a bullet is spawning it jumps to `0xe4c5`, the next slot, on every frame
and never reaches the graze test at `0xe3fb`. Only when the animation ends does
it become state 1 and enter the movement path that reaches it. State 5 is the
same in reverse, and state > 5 skips to `0xe4c5` directly. The kill test itself
is gated on `isGrazed` at `0xe475`.

`view.live_bullets` now filters, and every renderer and the oracle use it. The
remaining known gap: a spawning bullet will become lethal within a few frames,
and excluding it makes the oracle miss an imminent threat. That is consistent
with the approximation it already documents -- bullets that spawn during the
window are invisible -- but it is a real loss, and the alternative (drawing it,
marked as spawning) has not been built.

### The model does not read the board, and the crop does not ask it to

Playing runs cannot settle this. 500 decisions buys about 178 frames where the
answer could have been wrong, and the model's score on those sits three frames
from `always down` (144 vs 147, exact McNemar p = 0.77). `boardtest.py` builds
the boards instead, with ground truth from `oracle.py`, so no game is needed.

**The prior, with nothing to read.** On a board with no bullets at all the model
answered `left` 120 times out of 120, at 0.64 confidence. That is the baseline
every other number has to beat.

**One threat, one direction, every direction.** Each board carries a single
lethal move, so a constant policy scores (N-1)/N = 88% by construction and only
reading the board can beat it.

| board | model | best constant |
|---|---|---|
| single stationary bullet, 1 lone `o` | **50/120 (42%)** | 105/120 (88%) |
| column of three stationary bullets | 86/120 (72%) | 105/120 (88%) |
| single approaching bullet, 16-cell trail | 77/88 (88%) | 77/88 (88%) |

The model never beat the constant. On the single stationary bullet it moved
*into* the threat on 70 of 120 boards. Place one bullet directly above the player
and it moved up 15 times out of 15; above-left, up-left 15 out of 15. Confidence
was higher when it was wrong (median 0.94) than when it was right (0.87).

**The behaviour does not depend on the threat.** Rotating that same bullet 22.5
degrees off the axis puts it between two moves, so every path clears it while the
mark, the range and the glyph stay the same. Nothing is lethal on those boards,
yet the model still moves toward the mark 53% of the time overall and **82%** of
the time when the mark is above it -- against **80%** on the boards where moving
there would have killed it.

**Why.** `render_crop` draws each bullet's predicted positions for the next 16
frames. It never draws the player's own movement outcomes. A stationary bullet's
future is its own cell, so the prompt for a lethal stationary bullet and a
harmless one at the same range is often *byte-identical* -- 15 of 72 pairs in a
sample of 72. The model is not ignoring information; for those boards the
information is not there. Answering correctly requires simulating the player at
4.0 u/f across 12-unit cells, and it does not do that.

The one case that does carry a signal is an approaching bullet, whose trail runs
through the player's cell and replaces it with `+` or `!`. There the model scores
88% -- exactly what `always left` scores, so still no evidence of reading, but it
is no longer actively harmful.

**Consequence for the loop.** `oracle.py` computes the safe set exactly, in
arithmetic, with no API call. On every measure here it beats the model, which
costs ~130 ms per decision and reads the board less reliably than a constant. If
the model is to earn its place it needs either evidence that states the player's
movement outcomes rather than only the bullets' futures, or a job the oracle
cannot do -- bomb timing, risk appetite, spell-card strategy. Dodging, as
currently posed, is not that job.
