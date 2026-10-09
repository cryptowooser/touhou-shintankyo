# Convert a BMP to a downscaled PNG for viewing, with optional crop.
# usage: bmp2png.ps1 <in.bmp> <out.png> [maxWidth] [crop "x,y,w,h"]
param(
  [Parameter(Mandatory=$true)][string]$In,
  [Parameter(Mandatory=$true)][string]$Out,
  [int]$MaxWidth = 1400,
  [string]$Crop = ""
)
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile((Resolve-Path $In))

if ($Crop -ne "") {
  $p = $Crop.Split(',')
  $rect = New-Object System.Drawing.Rectangle ([int]$p[0]), ([int]$p[1]), ([int]$p[2]), ([int]$p[3])
} else {
  $rect = New-Object System.Drawing.Rectangle 0, 0, $img.Width, $img.Height
}

$w = [Math]::Min($MaxWidth, $rect.Width)
$h = [int]($rect.Height * $w / $rect.Width)
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.InterpolationMode = 'NearestNeighbor'
$dest = New-Object System.Drawing.Rectangle 0, 0, $w, $h
$g.DrawImage($img, $dest, $rect, [System.Drawing.GraphicsUnit]::Pixel)
$bmp.Save((Join-Path (Get-Location) $Out), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose(); $img.Dispose()
Write-Output "saved $Out ${w}x${h} from crop $($rect.X),$($rect.Y),$($rect.Width),$($rect.Height)"
