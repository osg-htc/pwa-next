pwa-next logo kit  (concept A: full mesh)
=========================================

The mark: five hosts, every pair connected. The perimeter is drawn heavier than the
diagonals so it reads as a mesh without clutter. The amber node with a faint halo is the
host/configuration being published. The wordmark is Inter Bold "pwa" + Inter SemiBold "-next"
(SIL Open Font License), converted to outlines, so no fonts are needed.

Colours
  Blue   #1565C0      Teal   #0FB5AE      Navy  #0B2A4A
  Amber  #FFB400      Teal on dark #5EEAD4   Teal text on light #0A8F8A

Folders
  icon/      Gradient tile (SVG, 512 and 1024 px PNG). Default app/avatar mark.
  favicon/   favicon.svg + favicon.ico (16/32/48) + PNGs 16/32/48/192/512 + apple-touch-icon.png.
             Heavier strokes and larger nodes than the main tile so it holds up when tiny.
  mark/      Tile-less mark: on-light, on-dark, mono-navy, mono-white (mono: amber becomes a ring).
  lockup/    horizontal and stacked, for light / for dark, tile-less "mark-only" and mono versions.
  social/    github-social-preview-1280x640 (upload under repo Settings > Social preview),
             avatar-500.png (full-bleed square, mark inside the circle-crop safe zone).

Using it in pwa-next
  The Branding page takes a logo image. Use lockup/horizontal-for-light.svg (or the PNG) for a
  light header, horizontal-for-dark for a dark one. Every SVG is plain vector shapes with no
  scripts, links, images or live text, so it is safe to serve under a restrictive
  Content-Security-Policy. The mark colours are fixed; the app's configurable brand colours do
  not recolour the SVG.

Guidelines
  Keep clear space around the mark of at least half the amber node's diameter.
  Below 24 px use favicon.svg, not the main tile.
  Do not recolour the amber node on the tile; use mono versions when one colour is required.
