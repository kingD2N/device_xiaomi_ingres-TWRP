#!/usr/bin/env python3
# Silences the "harmless" red errors TWRP shows after flashing a ROM on ingres
# (Virtual A/B). They are written to recovery.log as I: lines instead, the same
# way OrangeFox handles them (OF_REPORT_HARMLESS_MOUNT_ISSUES unset and
# OF_LOOP_DEVICE_ERRORS_TO_LOG).
#
#  1. Update_System_Details(): after a VAB flash, the partitions on super are
#     remapped and cannot be mounted until recovery reboots (TWRP itself warns
#     "Devices on super may not mount until rebooting recovery"), so each one
#     printed "Failed to mount '/vendor' (...)" etc. in red.
#  2. Prepare_Super_Volume(): same thing, while remapping super after a flash.
#  3. Unlock_Block_Partitions(): runs over every /dev/block node (loop, ram,
#     zram, ...) and printed "unable to open block device" in red. Also fixes
#     the missing strerror() argument in the "Unable to unlock" message.
#  4. TWPartition::Mount() (partition.cpp): the real cause of
#     "Failed to mount '/system_root' (Invalid argument)" (also system_ext,
#     product, vendor_dlkm). recovery.fstab lists each logical partition twice
#     (ext4 + erofs) but TWRP only keeps the first (ext4) one, and blkid cannot
#     always tell it the ROM is erofs, so mount(ext4) on an erofs image fails
#     with EINVAL. If both normal mount attempts fail, try the other read-only
#     filesystem (ext4 <-> erofs) before giving up.
#  5. gui/console.cpp: TWRP-only "no red" rule. Anything that would still be
#     printed in red (gui_err / LOGERR / kError messages) is shown in the
#     normal text colour with an "Error: " prefix instead. recovery.log keeps
#     the same line, so nothing is lost for debugging.
#
# Usage: python3 twrp_quiet_logs.py <path to bootable/recovery>
# Idempotent; prints a warning and leaves the file alone if an anchor is gone.

import re
import sys

MARK = "[ingres-quiet]"


def warn(msg):
    print(f"::warning::twrp_quiet_logs: {msg}")


def patch_function(src, signature, old, new, name):
    start = src.find(signature)
    if start < 0:
        warn(f"{name}: function not found, skipped")
        return src, False
    end = src.find("\n}\n", start)
    if end < 0:
        warn(f"{name}: end of function not found, skipped")
        return src, False
    body = src[start:end]
    if MARK in body:
        print(f"twrp_quiet_logs: {name}: already patched")
        return src, True
    if old not in body:
        warn(f"{name}: '{old.strip()}' not found, skipped")
        return src, False
    body = body.replace(old, new)
    return src[:start] + body + src[end:], True


def main():
    if len(sys.argv) != 2:
        print("usage: twrp_quiet_logs.py <bootable/recovery>")
        return 1
    path = sys.argv[1].rstrip("/") + "/partitionmanager.cpp"
    try:
        with open(path) as f:
            src = f.read()
    except OSError as e:
        warn(f"cannot read {path}: {e}")
        return 0

    src, _ = patch_function(
        src,
        "void TWPartitionManager::Update_System_Details(void) {",
        "(*iter)->Update_Size(true);",
        f"(*iter)->Update_Size(false); // {MARK}",
        "Update_System_Details")

    src, _ = patch_function(
        src,
        "bool TWPartitionManager::Prepare_Super_Volume(TWPartition* twrpPart) {",
        "twrpPart->Update_Size(true);",
        f"twrpPart->Update_Size(false); // {MARK}",
        "Prepare_Super_Volume")

    sig = "void TWPartitionManager::Unlock_Block_Partitions() {"
    start = src.find(sig)
    if start < 0:
        warn("Unlock_Block_Partitions: function not found, skipped")
    else:
        end = src.find("\n}\n", start)
        body = src[start:end]
        if MARK in body:
            print("twrp_quiet_logs: Unlock_Block_Partitions: already patched")
        else:
            new_body = body.replace(
                'LOGERR("unable to open block device',
                f'LOGINFO("{MARK} unable to open block device')
            new_body = re.sub(
                r'LOGERR\("Unable to unlock %s: %s\\n", block_device\.c_str\(\)(, strerror\(errno\))?\);',
                f'LOGINFO("{MARK} Unable to unlock %s: %s\\\\n", block_device.c_str(), strerror(errno));',
                new_body)
            if new_body == body:
                warn("Unlock_Block_Partitions: messages not found, skipped")
            src = src[:start] + new_body + src[end:]

    with open(path, "w") as f:
        f.write(src)
    print("twrp_quiet_logs: done ->", path)

    root = sys.argv[1].rstrip("/")
    patch_mount_fallback(root + "/partition.cpp")
    patch_console(root + "/gui/console.cpp")
    return 0


MOUNT_HELPER = f"""
// {MARK} ext4 <-> erofs fallback. recovery.fstab has both an ext4 and an erofs
// line for every logical partition, TWRP only keeps the first (ext4) one, and
// blkid does not always detect erofs -> mount() fails with EINVAL. Try the other
// filesystem before reporting an error. Returns true when the fallback also failed.
static bool ingres_alt_fs_failed(const string& dev, const string& mp, const string& fs,
		unsigned long flags, string& cur_fs) {{
	const char* alt = NULL;
	unsigned long alt_flags = flags;
	if (fs == "ext4") {{
		alt = "erofs";
		alt_flags |= MS_RDONLY;
	}} else if (fs == "erofs") {{
		alt = "ext4";
	}}
	if (alt == NULL)
		return true;
	int saved_errno = errno;
	if (mount(dev.c_str(), mp.c_str(), alt, alt_flags, NULL) == 0) {{
		LOGINFO("{MARK} '%s' mounted as %s (fstab/blkid said %s)\\n", mp.c_str(), alt, fs.c_str());
		cur_fs = alt;
		return false;
	}}
	errno = saved_errno;
	return true;
}}

"""


def patch_mount_fallback(path):
    try:
        with open(path) as f:
            src = f.read()
    except OSError as e:
        warn(f"cannot read {path}: {e}")
        return
    if MARK in src:
        print("twrp_quiet_logs: Mount ext4/erofs fallback: already patched")
        return
    sig = "bool TWPartition::Mount(bool Display_Error) {"
    old = ("\t\tmount(Actual_Block_Device.c_str(), Mount_Point.c_str(), mount_fs.c_str(), flags, NULL) != 0) {")
    new = ("\t\tmount(Actual_Block_Device.c_str(), Mount_Point.c_str(), mount_fs.c_str(), flags, NULL) != 0 &&\n"
           "\t\tingres_alt_fs_failed(Actual_Block_Device, Mount_Point, mount_fs, flags, Current_File_System)) {"
           f" // {MARK}")
    start = src.find(sig)
    if start < 0:
        warn("Mount: function not found, ext4/erofs fallback skipped")
        return
    end = src.find("\n}\n", start)
    body = src[start:end]
    if body.count(old) != 1:
        warn("Mount: mount() call not found, ext4/erofs fallback skipped")
        return
    body = body.replace(old, new)
    src = src[:start] + MOUNT_HELPER.lstrip("\n") + body + src[end:]
    with open(path, "w") as f:
        f.write(src)
    print("twrp_quiet_logs: Mount ext4/erofs fallback -> done")


CONSOLE_PRINT = f"""static void internal_gui_print(const char *color, char *buf)
{{
	// {MARK} no red text in TWRP: show errors as plain "Error: ..." lines.
	char ingres_buf[GUI_CONSOLE_BUFFER_SIZE + 8];
	if (color != NULL && strcmp(color, "error") == 0) {{
		const char *text = buf;
		if (strncmp(text, "E:", 2) == 0)
			text += 2;
		snprintf(ingres_buf, sizeof(ingres_buf), "Error: %s", text);
		buf = ingres_buf;
		color = "normal";
	}}
"""


def patch_console(path):
    try:
        with open(path) as f:
            src = f.read()
    except OSError as e:
        warn(f"cannot read {path}: {e}")
        return
    if MARK in src:
        print("twrp_quiet_logs: console no-red: already patched")
        return
    ok = True
    old1 = "static void internal_gui_print(const char *color, char *buf)\n{\n"
    if src.count(old1) != 1:
        warn("console: internal_gui_print not found")
        ok = False
    else:
        src = src.replace(old1, CONSOLE_PRINT)
    old2 = ('\t\tif (gMessages[m].GetKind() == msg::kError)\n'
            '\t\t\tcolor = "error";\n')
    new2 = ('\t\tif (gMessages[m].GetKind() == msg::kError) {\n'
            f'\t\t\tcolor = "normal"; // {MARK} no red, prefix instead\n'
            '\t\t\tmessage = "Error: " + message;\n'
            '\t\t}\n')
    if src.count(old2) != 1:
        warn("console: kError colour line not found")
        ok = False
    else:
        src = src.replace(old2, new2)
    with open(path, "w") as f:
        f.write(src)
    print("twrp_quiet_logs: console no-red ->", "done" if ok else "PARTIAL (see warnings)")


if __name__ == "__main__":
    sys.exit(main())
