"""Generate logo_160.png, favicon_32.png, and banner_1600x300.png from the TootFlow mascot."""
from PIL import Image, ImageDraw, ImageFont
import os

BG         = "#111b27"
TEAL       = "#47b8b1"
TEAL_DARK  = "#2d8f96"
WHITE      = "#ffffff"
BLACK      = "#141210"
LIGHT      = "#e8eef4"
MID        = "#a8b8c6"


def draw_mascot(draw, scale, ox, oy):
    def r(x, y, w, h, color):
        draw.rectangle(
            [ox + x*scale, oy + y*scale, ox + (x+w)*scale, oy + (y+h)*scale],
            fill=color,
        )
    def poly(pts, color):
        draw.polygon([(ox + px*scale, oy + py*scale) for px, py in pts], fill=color)

    # Arms / ear sides
    r(4,  14, 30, 40, TEAL)
    r(86, 14, 30, 40, TEAL)
    r(10, 20, 18, 26, TEAL_DARK)
    r(92, 20, 18, 26, TEAL_DARK)
    # Head
    r(22, 6,  76, 62, WHITE)
    # Eyes
    r(36, 20, 12, 12, BLACK)
    r(72, 20, 12, 12, BLACK)
    r(38, 22,  4,  4, WHITE)
    r(74, 22,  4,  4, WHITE)
    # Mouth / snout
    r(42, 44, 36, 20, LIGHT)
    r(50, 52,  7,  6, MID)
    r(63, 52,  7,  6, MID)
    # Body
    r(50, 68, 20, 26, WHITE)
    r(38, 86, 32, 10, WHITE)
    r(38, 90, 14,  6, WHITE)
    # Legs
    poly([(24,68),(18,68),(10,94),(18,94)], TEAL)
    poly([(96,68),(102,68),(110,94),(102,94)], TEAL)


# ── 160×160 logo ──────────────────────────────────────────────
PAD   = 12
scale = (160 - 2*PAD) / 120          # fit width; original viewBox is 120×104
ox    = PAD
oy    = (160 - 104 * scale) / 2

img = Image.new("RGBA", (160, 160), BG)
draw_mascot(ImageDraw.Draw(img), scale, ox, oy)
img.save("logo_160.png")
print("Saved logo_160.png")


# ── 32×32 favicon ─────────────────────────────────────────────
# Render at 4× (128×128) then downscale with LANCZOS for clean antialiasing
R     = 128
scale = (R - 8) / 120
ox    = 4
oy    = (R - 104 * scale) / 2

img_big = Image.new("RGBA", (R, R), BG)
draw_mascot(ImageDraw.Draw(img_big), scale, ox, oy)
img_small = img_big.resize((32, 32), Image.LANCZOS)
img_small.save("favicon_32.png")
print("Saved favicon_32.png")


# ── 1600×300 banner ───────────────────────────────────────────
W, H = 1600, 300

# Render at 2× then downscale for crispness
BW, BH = W * 2, H * 2

img_b = Image.new("RGBA", (BW, BH), BG)
draw  = ImageDraw.Draw(img_b)

# Subtle grid lines (matches landing page hero ::before)
GRID = 52
for x in range(0, BW, GRID):
    draw.line([(x, 0), (x, BH)], fill=(104, 194, 255, 20))
for y in range(0, BH, GRID):
    draw.line([(0, y), (BW, y)], fill=(104, 194, 255, 20))

# Teal accent bar at bottom (matches header border)
draw.rectangle([0, BH - 12, BW, BH], fill=TEAL)

# Mascot — fit height with padding
M_PAD  = 40
m_scale = (BH - M_PAD * 2) / 104
m_ox   = 60
m_oy   = (BH - 104 * m_scale) / 2
draw_mascot(draw, m_scale, m_ox, m_oy)

# Vertical divider after mascot
div_x = int(m_ox + 120 * m_scale + 60)
draw.rectangle([div_x, BH // 4, div_x + 4, BH * 3 // 4], fill=(42, 56, 72))

# Fonts — try system monospace, fall back to default
def load_font(size):
    candidates = [
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/Library/Fonts/CourierNew.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

font_title    = load_font(160)
font_sub      = load_font(68)
font_tagline  = load_font(52)

text_x = div_x + 80
# "TOOTFLOW" — teal
draw.text((text_x, BH // 2 - 130), "TOOTFLOW", font=font_title, fill=TEAL)
# Mastodon Campaign Planner — white
draw.text((text_x, BH // 2 + 58), "Mastodon Campaign Planner", font=font_sub, fill=WHITE)
# Tagline — muted
draw.text((text_x, BH // 2 + 160), "AI drafts · you decide · posts on schedule", font=font_tagline, fill="#5f6f80")

# Downscale to 1600×300
banner = img_b.resize((W, H), Image.LANCZOS)
banner.save("banner_1600x300.png")
print("Saved banner_1600x300.png")
