#!/usr/bin/env python3
"""Godox C100 firmware tool.

  python3 tools/c100.py dump                  save stock firmware
  python3 tools/c100.py dump --from-card      copy RESTORE off the card
  python3 tools/c100.py stage stock|muted|meow
  python3 tools/c100.py sounds --test-for shutter --5 long.wav --volume 70 --stage
  python3 tools/c100.py sounds --1 click.wav  replace slot(s), keep the rest
  python3 tools/c100.py extract-sounds        pull the five stock WAVs
  python3 tools/c100.py status
  python3 tools/c100.py gui                 desktop window

Never writes firmware/ORIGINAL/. Always leaves RESTORE_ORIGINAL.bin on the card.
Refuses any image that is not stock or stock with new sounds.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "firmware" / "ORIGINAL" / "gp_cardvr_upgrade.bin"
MEOW_BIN = ROOT / "firmware" / "gp_cardvr_upgrade.MEOW.bin"
MUTE_BIN = ROOT / "firmware" / "gp_cardvr_upgrade.MUTED.bin"
EXPECTED_ORIG = "f86b8c1c47b3ee29c494d38ecd5a5300cf36b2156a29d5aecb11f8a73ce922a3"
FW_SIZE = 1841152
CARD_CANDIDATES = ("Untitled 2", "Untitled", "NO NAME", "GODDX")
FLASH_HELP = (
    "Do not interrupt the flash. Do not remove the card. "
    "Do not remove power. Do not move the switch.\n"
    "The Godox logo comes up and holds for about 15 seconds. "
    "That is the flash. Then the camera turns off. "
    "Then the camera turns on with the new firmware."
)

# File offsets of the five embedded RIFF blobs (11.025 kHz mono).
# Resource pack at 0x170000 uses 512-byte clusters: BEEP=7, CAMERA=0xf,
# CLICK=0x1d, POWERON=0x202, TEST=0x238.
SLOTS = (
    {"n": 1, "name": "beep", "off": 0x170E00, "bits": 16, "ms": 182, "hint": "BEEP.WAV (menu) · 182 ms"},
    {"n": 2, "name": "camera", "off": 0x171E00, "bits": 16, "ms": 301, "hint": "CAMERA.WAV (shutter) · 301 ms"},
    {"n": 3, "name": "click", "off": 0x173A00, "bits": 16, "ms": 255, "hint": "CLICK.WAV (browse / keys) · 255 ms"},
    {"n": 4, "name": "poweron", "off": 0x1B0400, "bits": 16, "ms": 719, "hint": "POWERON_AUDIO.WAV · 719 ms"},
    {"n": 5, "name": "test", "off": 0x1B7000, "bits": 8, "ms": 1249, "hint": "TEST.WAV (unused) · 1.25 s"},
)


def vault_bytes() -> bytes:
    data = VAULT.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_ORIG:
        raise SystemExit("vault hash mismatch — refusing to use firmware/ORIGINAL/")
    return data


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _candidate_roots() -> list[Path]:
    if sys.platform == "win32":
        import ctypes
        DRIVE_REMOVABLE = 2
        seen: list[Path] = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if ctypes.windll.kernel32.GetDriveTypeW(str(root)) == DRIVE_REMOVABLE:
                seen.append(root)
        return seen
    vols = Path("/Volumes")
    if not vols.is_dir():
        return []
    named = [vols / name for name in CARD_CANDIDATES if (vols / name).is_dir()]
    return list(vols.iterdir()) + named


def find_card() -> Path | None:
    for p in _candidate_roots():
        if p.is_dir() and (p / "RESTORE_ORIGINAL.bin").is_file():
            return p
    for p in _candidate_roots():
        for name in CARD_CANDIDATES:
            if sys.platform == "win32":
                label = _win_drive_label(p)
                if label and label.upper() in [n.upper() for n in CARD_CANDIDATES]:
                    return p
            elif p.name in CARD_CANDIDATES:
                return p
    return None


def _win_drive_label(root: Path) -> str | None:
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.kernel32.GetVolumeInformationW(
            str(root), buf, len(buf), None, None, None, None, 0
        )
        return buf.value or None
    except Exception:
        return None


def require_card() -> Path:
    card = find_card()
    if card is None:
        raise SystemExit("camera card not mounted (expected RESTORE_ORIGINAL.bin on a removable drive)")
    return card


def require_restore(card: Path) -> None:
    restore = card / "RESTORE_ORIGINAL.bin"
    if not restore.is_file():
        raise SystemExit(f"no RESTORE_ORIGINAL.bin on {card}")
    if sha256(restore) != EXPECTED_ORIG:
        raise SystemExit("RESTORE_ORIGINAL.bin is not the stock vault image")


def stage_image(img: bytes, label: str, *, eject: bool) -> None:
    vault_bytes()
    assert_sound_only(img, what=label)
    card = require_card()
    require_restore(card)
    dest = card / "gp_cardvr_upgrade.bin"
    dest.write_bytes(img)
    try:
        with open(dest, "rb") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass
    if hashlib.sha256(dest.read_bytes()).digest() != hashlib.sha256(img).digest():
        dest.unlink(missing_ok=True)
        raise SystemExit("staged hash mismatch — removed the upgrade file")
    print(f"staged {label}  {hashlib.sha256(img).hexdigest()}")
    print(f"RESTORE left on {card / 'RESTORE_ORIGINAL.bin'}")
    if eject:
        ejected = False
        if sys.platform == "win32":
            try:
                letter = str(card)[:2]
                subprocess.check_call(
                    ["powershell", "-NoProfile", "-Command",
                     f"(New-Object -comObject Shell.Application)"
                     f".Namespace(17).ParseName('{letter}').InvokeVerb('Eject')"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                ejected = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        else:
            try:
                subprocess.check_call(["diskutil", "eject", str(card)])
                ejected = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass
        if ejected:
            print("ejected")
        else:
            print("eject the card manually, then flash")
    print(FLASH_HELP)


# --- WAV slots ----------------------------------------------------------------

def parse_wav_blob(blob: bytes) -> dict:
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise SystemExit("slot is not a RIFF WAVE")
    p = 12
    fmt = None
    data_off = data_sz = None
    while p + 8 <= len(blob):
        cid = blob[p : p + 4]
        csz = struct.unpack_from("<I", blob, p + 4)[0]
        if cid == b"fmt " and csz >= 16:
            audio_fmt, ch, rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", blob, p + 8)
            fmt = {"format": audio_fmt, "ch": ch, "rate": rate, "bits": bits}
        if cid == b"data":
            data_off, data_sz = p + 8, csz
            break
        p += 8 + csz + (csz & 1)
    if fmt is None or data_off is None:
        raise SystemExit("slot WAV missing fmt/data")
    return {"fmt": fmt, "data_off": data_off, "data_sz": data_sz, "blob_sz": 8 + struct.unpack_from("<I", blob, 4)[0]}


def slot_info(img: bytes, slot: dict) -> dict:
    # blob length from RIFF size; fall back to 64 KB window
    if img[slot["off"] : slot["off"] + 4] != b"RIFF":
        raise SystemExit(f"slot {slot['n']} @ {slot['off']:#x} is not RIFF")
    riff = struct.unpack_from("<I", img, slot["off"] + 4)[0]
    blob = img[slot["off"] : slot["off"] + 8 + riff]
    info = parse_wav_blob(blob)
    info.update(slot)
    info["frames"] = info["data_sz"] // max(1, info["fmt"]["bits"] // 8)
    info["ms"] = int(1000 * info["frames"] / info["fmt"]["rate"])
    return info


def write_wav(path: Path, pcm: bytes, rate: int, bits: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)


def _wave_pcm(src: Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(src), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def _s16_peak(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        peak = max(peak, abs(struct.unpack_from("<h", pcm, i)[0]))
    return peak


def _pcm_to_s16(pcm: bytes, bits: int) -> bytes | None:
    import audioop

    if bits == 16:
        return pcm if len(pcm) >= 2 else None
    if bits == 8:
        return audioop.lin2lin(audioop.bias(pcm, 1, -128), 1, 2)
    if bits == 32:
        return audioop.lin2lin(pcm, 4, 2)
    if bits == 24:
        n = len(pcm) // 3
        out = bytearray(n * 2)
        for i in range(n):
            j = i * 3
            v = pcm[j] | (pcm[j + 1] << 8) | (pcm[j + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            struct.pack_into("<h", out, i * 2, max(-32768, min(32767, v >> 8)))
        return bytes(out)
    return None


def _parse_user_wav(src: Path) -> tuple[int, int, int, bytes] | None:
    """Return (format_tag, ch, rate, s16_pcm) for integer PCM WAVs. Else None."""
    data = src.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    p = 12
    fmt_tag = ch = rate = bits = None
    pcm = None
    while p + 8 <= len(data):
        cid = data[p : p + 4]
        csz = struct.unpack_from("<I", data, p + 4)[0]
        body = data[p + 8 : p + 8 + csz]
        if cid == b"fmt " and csz >= 16:
            fmt_tag, ch, rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", body, 0)
            if fmt_tag == 0xFFFE and csz >= 40:
                fmt_tag = struct.unpack_from("<H", body, 24)[0]
        elif cid == b"data":
            pcm = body
            break
        p += 8 + csz + (csz & 1)
    if None in (fmt_tag, ch, rate, bits) or not pcm:
        return None
    if fmt_tag != 1 or ch not in (1, 2):
        return None
    s16 = _pcm_to_s16(pcm, bits)
    if not s16:
        return None
    return fmt_tag, ch, rate, s16


def _smart_mono(s16: bytes, ch: int) -> bytes:
    """Stereo → mono. If L+R cancel, keep the louder channel."""
    import audioop

    if ch != 2:
        return s16
    left = audioop.tomono(s16, 2, 1, 0)
    right = audioop.tomono(s16, 2, 0, 1)
    avg = audioop.tomono(s16, 2, 0.5, 0.5)
    pl, pr, pa = _s16_peak(left), _s16_peak(right), _s16_peak(avg)
    if pa < max(pl, pr) * 0.25:
        return left if pl >= pr else right
    return avg


def _ffmpeg_s16_mono_11025(src: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit(f"cannot read {src} (install ffmpeg for non-WAV audio)")
    proc = subprocess.run(
        [
            ffmpeg, "-v", "error", "-i", str(src),
            "-ac", "1", "-ar", "11025", "-c:a", "pcm_s16le",
            "-f", "s16le", "pipe:1",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or len(proc.stdout) < 4:
        raise SystemExit(
            f"ffmpeg failed on {src}:\n{proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout


def _s16_mono_11025(src: Path) -> bytes:
    """Load any audio as signed 16-bit LE mono @ 11025 Hz."""
    parsed = None
    try:
        parsed = _parse_user_wav(src)
    except OSError:
        parsed = None
    if parsed:
        _tag, ch, rate, s16 = parsed
        s16 = _smart_mono(s16, ch)
        if rate != 11025:
            import audioop

            s16, _ = audioop.ratecv(s16, 2, 1, rate, 11025, None)
        if len(s16) >= 2:
            return s16
    return _ffmpeg_s16_mono_11025(src)


def _pad_to(pcm: bytes, nbytes: int, fill: bytes) -> bytes:
    """Fit PCM to the slot: truncate, or pad with silence (0x00 / 0x80)."""
    if not pcm:
        raise SystemExit("empty audio")
    if len(pcm) >= nbytes:
        return pcm[:nbytes]
    return pcm + fill * (nbytes - len(pcm))


def _s16_to_u8(pcm: bytes) -> bytes:
    out = bytearray(len(pcm) // 2)
    for i in range(len(out)):
        s = struct.unpack_from("<h", pcm, i * 2)[0]
        out[i] = (s + 32768) >> 8
    return bytes(out)


def make_wav(pcm: bytes, rate: int, bits: int) -> bytes:
    block = bits // 8
    data_sz = len(pcm)
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_sz,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * block,
        block,
        bits,
        b"data",
        data_sz,
    )
    return hdr + pcm


def _is_meow_wav(src: Path) -> bool:
    """True if this is the repo meow.wav that built the working MEOW.bin."""
    try:
        if src.resolve() == (ROOT / "sounds" / "meow.wav").resolve():
            return True
    except OSError:
        pass
    try:
        return hashlib.sha256(src.read_bytes()).hexdigest().startswith("64d46f8362fd2c48")
    except OSError:
        return False


def pcm_for_slot(src: Path, want_rate: int, want_width: int, want_bytes: int) -> bytes:
    """Pack custom audio the same way the working MEOW.bin was built.

    audioop stereo-average → 11025 Hz, then pad/truncate from the start.
    Trim/window/normalize made meow.wav itself silent on camera.
    """
    del want_rate
    s16 = _s16_mono_11025(src)
    if _s16_peak(s16) < 64:
        raise SystemExit(
            f"{src}: no audible audio after convert. Use a louder clip, or Mute."
        )
    if want_width == 1:
        return _pad_to(_s16_to_u8(s16), want_bytes, b"\x80")
    return _pad_to(s16, want_bytes, b"\x00")


def base_image() -> bytearray:
    return bytearray(vault_bytes())


def apply_volume(pcm: bytes, bits: int, volume: float) -> bytes:
    """Scale PCM. volume is a gain (1.0 = unchanged). Clamped to the sample range."""
    if abs(volume - 1.0) < 1e-6:
        return pcm
    if volume <= 0:
        raise SystemExit("volume must be > 0")
    out = bytearray(len(pcm))
    if bits == 8:
        for i, b in enumerate(pcm):
            s = int((b - 128) * volume)
            out[i] = max(0, min(255, s + 128))
        return bytes(out)
    for i in range(0, len(pcm) - 1, 2):
        s = int(struct.unpack_from("<h", pcm, i)[0] * volume)
        struct.pack_into("<h", out, i, max(-32768, min(32767, s)))
    return bytes(out)


def parse_volume(raw) -> float:
    """CLI/GUI volume is a percent: 50 = half, 100 = unchanged, 200 = double."""
    if raw is None:
        return 1.0
    x = float(raw)
    if x <= 0 or x > 400:
        raise SystemExit("volume is percent, 1–400 (100 = unchanged)")
    return x / 100.0


def patch_slots(
    img: bytearray,
    replacements: dict[int, Path | str],
    volume: float = 1.0,
) -> list[str]:
    """Patch PCM only. Keep the stock RIFF/LIST headers (mute and MEOW.bin do this).

    replacements: slot number → Path, 'mute', or 'meow'
    """
    meow_img = MEOW_BIN.read_bytes() if any(v == "meow" for v in replacements.values()) else None
    log = []
    for slot in SLOTS:
        if slot["n"] not in replacements:
            continue
        info = slot_info(bytes(img), slot)
        spec = replacements[slot["n"]]
        bits = info["fmt"]["bits"]
        off = slot["off"] + info["data_off"]
        n = info["data_sz"]
        if spec == "mute":
            fill = b"\x80" if bits == 8 else b"\x00"
            pcm = fill * n
            src = "silence"
        elif spec == "meow" or (
            spec not in ("mute", "meow") and Path(spec).is_file() and _is_meow_wav(Path(spec))
        ):
            if bits == 8:
                if meow_img is None:
                    meow_img = MEOW_BIN.read_bytes()
                pcm = meow_img[off : off + n]
                src = "MEOW.bin"
            else:
                pcm = pcm_for_slot(ROOT / "sounds" / "meow.wav", 11025, 2, n)
                src = "meow.wav"
        else:
            path = Path(spec)
            if not path.is_file():
                raise SystemExit(f"slot {slot['n']}: not a file: {path}")
            pcm = pcm_for_slot(path, info["fmt"]["rate"], bits // 8, n)
            src = str(path)
        if len(pcm) != n:
            raise SystemExit(f"slot {slot['n']}: pcm {len(pcm)} != {n}")
        if spec != "mute":
            pcm = apply_volume(pcm, bits, volume)
        img[off : off + n] = pcm
        extra = ""
        if spec != "mute":
            if bits == 16:
                peak = max(abs(struct.unpack_from("<h", pcm, i)[0]) for i in range(0, min(n, 8000) - 1, 2))
            else:
                peak = max(abs(b - 128) for b in pcm[:4000])
            extra = f"  peak={peak}"
            if abs(volume - 1.0) >= 1e-6:
                extra += f"  vol={volume:.2f}x"
        log.append(
            f"slot {slot['n']} ({slot['hint']}, {info['ms']} ms, {bits}-bit) ← {src}{extra}"
        )
    return log


# play() opens these names at boot. TEST.WAV is 1.25 s and nothing uses it.
_PLAY_NAME = {
    "shutter": (0xE1B80, b"CAMERA.WAV"),
    "poweron": (0xE1B98, b"POWERON_AUDIO.WAV"),
}


# Stock TEST.WAV is 8-bit (never played). Same 23480-byte hole holds a
# 16-bit 11025 Hz WAV of 1063 ms — power-on/shutter expect 16-bit.
TEST_BLOB = 23480
TEST_S16_DATA = TEST_BLOB - 44  # 23436, even


def rebuild_test_as_s16(img: bytearray) -> None:
    """Replace the unused 8-bit TEST blob with a 16-bit WAV in the same hole."""
    slot = SLOTS[4]
    off = slot["off"]
    try:
        info = slot_info(bytes(img), slot)
        if info["fmt"]["bits"] == 16 and info["data_sz"] == TEST_S16_DATA:
            return
    except SystemExit:
        pass
    blob = make_wav(b"\x00" * TEST_S16_DATA, 11025, 16)
    if len(blob) != TEST_BLOB:
        raise SystemExit(f"TEST 16-bit wav {len(blob)} != {TEST_BLOB}")
    img[off : off + TEST_BLOB] = blob
    print("TEST slot rebuilt as 16-bit 1063 ms (stock 8-bit was choppy on play)")


def retarget_play_to_test(img: bytearray, which: str) -> None:
    """Make shutter or power-on open TEST.WAV instead."""
    if which not in _PLAY_NAME:
        raise SystemExit(f"test-for must be shutter or poweron, not {which!r}")
    off, old = _PLAY_NAME[which]
    new = b"TEST.WAV" + b"\x00" * (len(old) - len(b"TEST.WAV"))
    cur = bytes(img[off : off + len(old)])
    if cur.startswith(b"TEST.WAV\x00"):
        print(f"{which} already plays TEST.WAV")
        return
    if cur != old:
        raise SystemExit(f"{which} name at {off:#x} is {cur!r}, expected {old!r}")
    img[off : off + len(new)] = new
    print(f"{which} now plays TEST.WAV (16-bit 1063 ms, slot 5)")


def route_reps_to_test(
    reps: dict[int, Path | str] | None, which: str
) -> dict[int, Path | str] | None:
    """A WAV on the short shutter/power-on row is written into the TEST slot."""
    if not reps:
        return reps
    src = 2 if which == "shutter" else 4
    if src in reps and 5 not in reps:
        reps = dict(reps)
        reps[5] = reps.pop(src)
        print(f"slot {src} audio → TEST slot 5 ({which})")
    return reps


def _diff_runs(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """Inclusive-start, exclusive-end byte runs where a and b differ."""
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(a)
    mv_a = memoryview(a)
    mv_b = memoryview(b)
    while i < n:
        if mv_a[i] == mv_b[i]:
            i += 1
            continue
        j = i + 1
        while j < n and mv_a[j] != mv_b[j]:
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _allowed_sound_ranges(vault: bytes) -> list[tuple[int, int]]:
    """Byte ranges a sound build may change: WAV blobs and play() names."""
    ranges = []
    for slot in SLOTS:
        info = slot_info(vault, slot)
        end = slot["off"] + info["blob_sz"]
        if slot["n"] == 5:
            end = slot["off"] + TEST_BLOB
        ranges.append((slot["off"], end))
    for _which, (off, old) in _PLAY_NAME.items():
        ranges.append((off, off + len(old)))
    return ranges


def _range_covers(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= start and end <= hi for lo, hi in ranges)


def _play_name_ok(img: bytes) -> list[str]:
    bad = []
    for which, (off, old) in _PLAY_NAME.items():
        cur = bytes(img[off : off + len(old)])
        new = b"TEST.WAV" + b"\x00" * (len(old) - len(b"TEST.WAV"))
        if cur not in (old, new):
            bad.append(f"{which} name at {off:#x} is {cur!r}, not stock or TEST.WAV")
    return bad


def _slots_ok(img: bytes) -> list[str]:
    bad = []
    for slot in SLOTS:
        try:
            info = slot_info(img, slot)
        except SystemExit as e:
            bad.append(f"slot {slot['n']}: {e}")
            continue
        fmt = info["fmt"]
        if fmt["format"] != 1 or fmt["ch"] != 1 or fmt["rate"] != 11025:
            bad.append(
                f"slot {slot['n']}: need PCM mono 11025 Hz, got "
                f"fmt={fmt['format']} ch={fmt['ch']} rate={fmt['rate']}"
            )
        if slot["n"] < 5 and fmt["bits"] != 16:
            bad.append(f"slot {slot['n']}: need 16-bit, got {fmt['bits']}")
        if slot["n"] == 5 and fmt["bits"] not in (8, 16):
            bad.append(f"slot 5: need 8-bit or 16-bit, got {fmt['bits']}")
        if slot["n"] == 5 and info["blob_sz"] != TEST_BLOB:
            bad.append(f"slot 5: blob {info['blob_sz']} != {TEST_BLOB}")
    return bad


def sound_only_violations(img: bytes) -> list[str]:
    """Why this image is not stock-or-sounds. Empty list means safe to flash."""
    vault = vault_bytes()
    bad: list[str] = []
    if len(img) != FW_SIZE:
        bad.append(f"size {len(img)} != {FW_SIZE}")
        return bad
    if len(vault) != FW_SIZE:
        bad.append("vault size is not the stock image")
        return bad
    if img == vault:
        return []
    allowed = _allowed_sound_ranges(vault)
    for start, end in _diff_runs(img, vault):
        if not _range_covers(start, end, allowed):
            bad.append(f"differs from stock at {start:#x}..{end:#x} (not a sound slot)")
    bad.extend(_play_name_ok(img))
    bad.extend(_slots_ok(img))
    return bad


def assert_sound_only(img: bytes, *, what: str = "firmware") -> None:
    """Refuse any image that changes code, extras, or anything but sounds.

    Stock is allowed. Sound builds may change WAV PCM (and rebuild TEST.WAV)
    and may retarget shutter/power-on to TEST.WAV. Nothing else.
    """
    if img == vault_bytes():
        return
    bad = sound_only_violations(img)
    if bad:
        raise SystemExit(
            f"{what} is not stock or sound-only — refusing to write it:\n"
            + "\n".join(f"  {line}" for line in bad)
        )


def compose_firmware(
    *,
    reps: dict[int, Path | str] | None = None,
    test_for: str | None = None,
    volume: float = 1.0,
) -> bytes:
    img = base_image()
    print("base stock")
    if abs(volume - 1.0) >= 1e-6:
        print(f"volume {volume:.2f}x ({int(round(volume * 100))}%)")
    if test_for:
        retarget_play_to_test(img, test_for)
        rebuild_test_as_s16(img)
        reps = route_reps_to_test(reps, test_for)
    if reps:
        for line in patch_slots(img, reps, volume=volume):
            print(line)
    data = bytes(img)
    assert_sound_only(data, what="composed firmware")
    return data


# --- commands -----------------------------------------------------------------

def cmd_dump(args: argparse.Namespace) -> None:
    out = Path(args.out).expanduser() if args.out else ROOT / "captures" / "gp_cardvr_upgrade.ORIGINAL.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.from_card:
        card = require_card()
        src = card / "RESTORE_ORIGINAL.bin"
        if not src.is_file():
            raise SystemExit("no RESTORE_ORIGINAL.bin on the card")
        data = src.read_bytes()
        if hashlib.sha256(data).hexdigest() != EXPECTED_ORIG:
            raise SystemExit("card RESTORE is not stock — not writing it as original")
        label = "card RESTORE_ORIGINAL.bin"
    else:
        data = vault_bytes()
        label = "firmware/ORIGINAL/"
    out.write_bytes(data)
    print(f"wrote {out}  ({len(data)} bytes)")
    print(f"source {label}")
    print(f"sha256 {hashlib.sha256(data).hexdigest()}")


def cmd_stage(args: argparse.Namespace) -> None:
    test_for = getattr(args, "test_for", None) or None
    volume = parse_volume(getattr(args, "volume", None) or 100)
    what = args.image
    if what == "yuv":
        raise SystemExit("YUV firmware is removed from this tool")
    elif what == "stock":
        data = vault_bytes()
        label = "stock"
    elif what == "muted":
        data = compose_firmware(reps={n: "mute" for n in range(1, 6)}, test_for=test_for)
        label = "MUTED"
    elif what == "meow":
        data = compose_firmware(
            reps={n: "meow" for n in range(1, 6)}, test_for=test_for, volume=volume
        )
        label = "MEOW"
    else:
        p = Path(what).expanduser()
        if not p.is_file():
            raise SystemExit(f"not a file: {p}")
        data = p.read_bytes()
        if len(data) != FW_SIZE:
            raise SystemExit(f"{p} is {len(data)} bytes, expected {FW_SIZE}")
        label = p.name
    stage_image(data, label, eject=not args.keep)


def cmd_clean_card(_args: argparse.Namespace) -> None:
    """Delete old experiment DUMP*.BIN files. Keep RESTORE, JPEGs, YUV00+."""
    card = require_card()
    removed = 0
    kept = 0
    for p in list(card.iterdir()):
        if not p.is_file():
            continue
        name = p.name.upper()
        if name.startswith("YUV") and name.endswith(".BIN"):
            kept += 1
            continue
        junk = name.startswith("DUMP") or name.startswith("PEEK") or name.startswith("RAW")
        if not (junk and name.endswith(".BIN")):
            continue
        sz = p.stat().st_size
        p.unlink()
        print(f"removed {p.name}  ({sz} bytes)")
        removed += 1
    print(f"removed {removed} old DUMP*.BIN  kept {kept} YUV*.BIN")
    print("RESTORE / JPEGs / upgrade file left in place")


def cmd_extract_sounds(args: argparse.Namespace) -> None:
    img = vault_bytes()
    out = Path(args.out).expanduser() if args.out else ROOT / "captures" / "sounds_stock"
    out.mkdir(parents=True, exist_ok=True)
    for slot in SLOTS:
        info = slot_info(img, slot)
        pcm = img[slot["off"] + info["data_off"] : slot["off"] + info["data_off"] + info["data_sz"]]
        dest = out / f"{slot['n']:02d}_{slot['name']}.wav"
        write_wav(dest, pcm, info["fmt"]["rate"], info["fmt"]["bits"])
        print(f"{dest.name:24} {info['ms']:4} ms  {info['fmt']['bits']}-bit  {info['frames']} frames  ({slot['hint']})")
    print("wrote", out)


def cmd_sounds(args: argparse.Namespace) -> None:
    reps: dict[int, Path | str] = {}
    for n in range(1, 6):
        val = getattr(args, f"s{n}")
        if val:
            reps[n] = Path(val)
    if args.mute:
        for part in args.mute.split(","):
            part = part.strip()
            if part in ("all", "*"):
                for n in range(1, 6):
                    reps.setdefault(n, "mute")
                break
            n = int(part)
            if n not in range(1, 6):
                raise SystemExit(f"slot must be 1-5, got {n}")
            reps[n] = "mute"
    if getattr(args, "meow", False):
        for n in range(1, 6):
            reps.setdefault(n, "meow")
    test_for = getattr(args, "test_for", None) or None
    if not reps and not test_for:
        raise SystemExit("nothing to do: pass --test-for and/or --1 FILE … --5 FILE / --mute / --meow")
    if args.base:
        img = bytearray(Path(args.base).read_bytes())
        if len(img) != FW_SIZE:
            raise SystemExit(f"base image must be {FW_SIZE} bytes")
        assert_sound_only(bytes(img), what=args.base)
        volume = parse_volume(getattr(args, "volume", None) or 100)
        if test_for:
            retarget_play_to_test(img, test_for)
            rebuild_test_as_s16(img)
            reps = route_reps_to_test(reps, test_for)
        if reps:
            for line in patch_slots(img, reps, volume=volume):
                print(line)
        data = bytes(img)
        assert_sound_only(data, what="composed firmware")
    else:
        volume = parse_volume(getattr(args, "volume", None) or 100)
        data = compose_firmware(reps=reps or None, test_for=test_for, volume=volume)
    out = Path(args.out).expanduser() if args.out else ROOT / "firmware" / "gp_cardvr_upgrade.SOUNDS.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print("wrote", out, hashlib.sha256(data).hexdigest())
    if args.stage:
        stage_image(data, out.name, eject=not args.keep)


def cmd_mute(args: argparse.Namespace) -> None:
    slots = list(range(1, 6))
    if args.slots:
        slots = []
        for part in args.slots.split(","):
            n = int(part.strip())
            if n not in range(1, 6):
                raise SystemExit(f"slot must be 1-5, got {n}")
            slots.append(n)
    data = compose_firmware(reps={n: "mute" for n in slots})
    out = Path(args.out).expanduser() if args.out else None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print("wrote", out, hashlib.sha256(data).hexdigest())
    if args.out and not args.stage:
        return
    stage_image(data, "muted", eject=not args.keep)


def cmd_status(_args: argparse.Namespace) -> None:
    v = vault_bytes()
    print(f"vault     {VAULT}  {EXPECTED_ORIG}")
    card = find_card()
    if card is None:
        print("card      not mounted")
        return
    print(f"card      {card}")
    restore = card / "RESTORE_ORIGINAL.bin"
    if restore.is_file():
        ok = sha256(restore) == EXPECTED_ORIG
        print(f"RESTORE   {'ok stock' if ok else 'HASH MISMATCH'}")
    else:
        print("RESTORE   missing")
    up = card / "gp_cardvr_upgrade.bin"
    print(f"upgrade   {sha256(up)[:16]}…" if up.is_file() else "upgrade   (none — last flash ate it)")
    print("slots in vault:")
    for slot in SLOTS:
        info = slot_info(v, slot)
        print(f"  {slot['n']}  {slot['name']:8}  {info['ms']:4} ms  {info['fmt']['bits']:2}-bit  @{slot['off']:#x}")


def cmd_gui(_args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import c100_gui

    c100_gui.main()


def cmd_menu(_args: argparse.Namespace) -> None:
    print("C100 firmware tool")
    print("  1  dump original firmware")
    print("  2  replace sounds")
    print("  3  mute all sounds (write to card)")
    print("  4  write stock / muted / meow to card")
    print("  5  status")
    print("  q  quit")
    choice = input("> ").strip().lower()
    if choice == "1":
        cmd_dump(argparse.Namespace(out=None, from_card=False))
    elif choice == "2":
        print("Enter a WAV for each slot, or blank to leave it. 'mute' silences.")
        reps: dict[int, Path | str] = {}
        img = vault_bytes()
        for slot in SLOTS:
            info = slot_info(img, slot)
            raw = input(f"  slot {slot['n']} {slot['hint']} ({info['ms']} ms, {info['fmt']['bits']}-bit): ").strip()
            if not raw:
                continue
            reps[slot["n"]] = "mute" if raw.lower() == "mute" else Path(raw)
        if not reps:
            print("no changes")
            return
        ns = argparse.Namespace(
            s1=None, s2=None, s3=None, s4=None, s5=None,
            mute=None, meow=False, out=None, base=None,
            stage=False, keep=False, test_for=None, volume=100,
        )
        for n, val in reps.items():
            if val == "mute":
                ns.mute = (str(ns.mute) + "," if ns.mute else "") + str(n)
            else:
                setattr(ns, f"s{n}", str(val))
        cmd_sounds(ns)
        if find_card() is not None:
            if input("stage onto card? [y/N] ").strip().lower() == "y":
                cmd_stage(argparse.Namespace(image=str(ROOT / "firmware" / "gp_cardvr_upgrade.SOUNDS.bin"), keep=False))
    elif choice == "3":
        cmd_mute(argparse.Namespace(slots=None, out=None, stage=True, keep=False))
    elif choice == "4":
        which = input("stock / muted / meow: ").strip().lower()
        if which not in ("stock", "muted", "meow"):
            raise SystemExit("pick stock, muted, or meow")
        cmd_stage(argparse.Namespace(image=which, keep=False))
    elif choice in ("5", "s"):
        cmd_status(argparse.Namespace())
    elif choice in ("q", ""):
        return
    else:
        raise SystemExit("unknown choice")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c100", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("dump", help="save stock firmware to a file")
    d.add_argument("-o", "--out", help="output path")
    d.add_argument("--from-card", action="store_true", help="copy RESTORE_ORIGINAL.bin off the card")
    d.set_defaults(func=cmd_dump)

    s = sub.add_parser("stage", help="write a firmware image onto the card")
    s.add_argument("image", help="stock | muted | meow | path/to.bin")
    s.add_argument(
        "--test-for",
        choices=("shutter", "poweron"),
        help="play the unused 1.25 s TEST slot for shutter or power-on",
    )
    s.add_argument("--volume", type=float, default=100, help="sound volume percent (100 = unchanged)")
    s.add_argument("--keep", action="store_true", help="do not eject")
    s.set_defaults(func=cmd_stage)

    cl = sub.add_parser("clean-card", help="delete old experiment DUMP*.BIN (keeps YUV00+ / RESTORE / JPEGs)")
    cl.set_defaults(func=cmd_clean_card)

    so = sub.add_parser("sounds", help="replace any of the five 11.025 kHz slots")
    for n, slot in enumerate(SLOTS, 1):
        so.add_argument(f"--{n}", dest=f"s{n}", metavar="WAV", help=f"slot {n}: {slot['hint']}")
    so.add_argument("--mute", metavar="LIST", help="silence slots, e.g. 1,5 or all")
    so.add_argument("--meow", action="store_true", help="fill unused slots from MEOW.bin")
    so.add_argument(
        "--test-for",
        choices=("shutter", "poweron"),
        help="play the unused 1.25 s TEST slot for shutter or power-on",
    )
    so.add_argument("--volume", type=float, default=100, help="sound volume percent (100 = unchanged)")
    so.add_argument("-o", "--out", help="output firmware (default firmware/gp_cardvr_upgrade.SOUNDS.bin)")
    so.add_argument("--base", help="image to patch (default: vault stock)")
    so.add_argument("--stage", action="store_true", help="also write the result to the card")
    so.add_argument("--keep", action="store_true")
    so.set_defaults(func=cmd_sounds)

    mu = sub.add_parser("mute", help="silence sounds and write the image to the card")
    mu.add_argument("--slots", metavar="LIST", help="only these slots, e.g. 1,5 (default: all five)")
    mu.add_argument("-o", "--out", help="also save the image to this path")
    mu.add_argument("--stage", action="store_true", help="write to the card even when -o is set")
    mu.add_argument("--keep", action="store_true", help="do not eject")
    mu.set_defaults(func=cmd_mute)

    x = sub.add_parser("extract-sounds", help="export the five stock WAVs")
    x.add_argument("-o", "--out", help="output directory")
    x.set_defaults(func=cmd_extract_sounds)

    st = sub.add_parser("status", help="vault + card")
    st.set_defaults(func=cmd_status)

    g = sub.add_parser("gui", help="desktop window")
    g.set_defaults(func=cmd_gui)
    return p


def main() -> None:
    if len(sys.argv) == 1 and sys.stdin.isatty():
        cmd_menu(argparse.Namespace())
        return
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
