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
    return 0


if __name__ == "__main__":
    sys.exit(main())
