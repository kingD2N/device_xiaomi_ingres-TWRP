#!/usr/bin/env python3
# Tema "Monet Hijau" untuk TWRP ingres (sama dengan WebUI modul kingD2N).
#
#  - Warna tema (ui.xml portrait_hdpi): header/tombol hijau #2E7D32, sorotan
#    #8DD98F, latar gelap kehijauan #1A1C19, teks #E2E3DD.
#  - Gambar yang tadinya biru TWRP (#0090CA: checkbox, radio, slider, progress,
#    handle, ikon file, tombol enter, splash) diganti versi hijau dari
#    patches/theme_monet/<tema>/images (sudah diwarnai ulang, tidak perlu Pillow).
#
# Usage: python3 twrp_theme_monet.py <path ke bootable/recovery> [tema]
# Tema bawaan portrait_hdpi (dipilih otomatis TWRP untuk layar 1080x2400).
# Aman dijalankan berulang.

import os
import re
import shutil
import sys

MARK = "ingres-monet"

COLORS = {
    "background_color": "#1A1C19",
    "accent_color": "#2E7D32",
    "accent_color_semitransparent": "#2E7D3230",
    "text_color": "#E2E3DD",
    "text_button_color": "#E2E3DD",
    "text_success_color": "#8DD98F",
    "highlight_color": "#1A1C1980",
    "caps_highlight_color": "#2E7D3240",
    "highlight": "#8DD98F",
    "fileselector_linecolor": "#424940",
    "fileselector_highlight_color": "#424940",
}

# warna yang ditulis langsung (bukan variabel) di ui.xml
LITERALS = {
    'ctrlhighlight color="#0090CA80"': 'ctrlhighlight color="#2E7D3280"',
    'key-alphanumeric color="#111111"': 'key-alphanumeric color="#1F2A1F"',
}


def warn(msg):
    print(f"::warning::twrp_theme_monet: {msg}")


def main():
    if len(sys.argv) < 2:
        print("usage: twrp_theme_monet.py <bootable/recovery> [tema]")
        return 1
    recovery = sys.argv[1].rstrip("/")
    theme = sys.argv[2] if len(sys.argv) > 2 else "portrait_hdpi"
    theme_dir = os.path.join(recovery, "gui", "theme", theme)
    ui = os.path.join(theme_dir, "ui.xml")
    try:
        with open(ui) as f:
            src = f.read()
    except OSError as e:
        warn(f"tidak bisa baca {ui}: {e}")
        return 0

    changed = 0
    for name, value in COLORS.items():
        pat = re.compile(r'(<variable name="%s" value=")[^"]*(")' % re.escape(name))
        src, n = pat.subn(r"\g<1>%s\g<2>" % value, src)
        if n == 0:
            warn(f"variabel {name} tidak ada di {theme}/ui.xml")
        changed += n
    for old, new in LITERALS.items():
        changed += src.count(old)
        src = src.replace(old, new)
    if f"<!-- {MARK} -->" not in src:
        src = src.replace("<variables>", f"<variables>\n\t\t<!-- {MARK} -->", 1)
    with open(ui, "w") as f:
        f.write(src)
    print(f"twrp_theme_monet: {theme}/ui.xml -> {changed} warna")

    here = os.path.dirname(os.path.abspath(__file__))
    img_src = os.path.join(here, "theme_monet", theme, "images")
    img_dst = os.path.join(theme_dir, "images")
    copied = 0
    if os.path.isdir(img_src):
        for name in sorted(os.listdir(img_src)):
            if not name.endswith(".png"):
                continue
            dst = os.path.join(img_dst, name)
            if not os.path.exists(dst):
                warn(f"{name} tidak ada di source TWRP, dilewati")
                continue
            shutil.copyfile(os.path.join(img_src, name), dst)
            copied += 1
    else:
        warn(f"folder gambar {img_src} tidak ada, gambar tetap biru")
    print(f"twrp_theme_monet: {copied} gambar hijau dipasang")
    return 0


if __name__ == "__main__":
    sys.exit(main())
