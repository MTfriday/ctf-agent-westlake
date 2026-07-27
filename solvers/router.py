"""Challenge classification router — determines challenge type from metadata."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any


class ChallengeCategory(str, Enum):
    REV = "rev"
    PWN = "pwn"
    WEB = "web"
    CRYPTO = "crypto"
    MISC = "misc"
    FORENSICS = "forensics"
    OSINT = "osint"
    UNKNOWN = "unknown"

    @classmethod
    def from_str(cls, s: str) -> ChallengeCategory:
        """Parse a category string, handling various naming conventions."""
        normalized = s.lower().strip()
        mapping = {
            "reverse": cls.REV,
            "reversing": cls.REV,
            "re": cls.REV,
            "rev": cls.REV,
            "pwn": cls.PWN,
            "binary": cls.PWN,
            "exploitation": cls.PWN,
            "pwnable": cls.PWN,
            "web": cls.WEB,
            "web exploitation": cls.WEB,
            "crypto": cls.CRYPTO,
            "cryptography": cls.CRYPTO,
            "cryptanalysis": cls.CRYPTO,
            "misc": cls.MISC,
            "miscellaneous": cls.MISC,
            "forensics": cls.FORENSICS,
            "forensic": cls.FORENSICS,
            "foren": cls.FORENSICS,
            "osint": cls.OSINT,
            "osint (open source intelligence)": cls.OSINT,
        }
        # Exact match first
        if normalized in mapping:
            return mapping[normalized]
        # Partial match
        for key, cat in mapping.items():
            if key in normalized or normalized in key:
                return cat
        return cls.UNKNOWN

    def display_name(self) -> str:
        names = {
            self.REV: "Reverse Engineering",
            self.PWN: "Binary Exploitation",
            self.WEB: "Web Security",
            self.CRYPTO: "Cryptography",
            self.MISC: "Miscellaneous",
            self.FORENSICS: "Digital Forensics",
            self.OSINT: "OSINT",
            self.UNKNOWN: "Unknown",
        }
        return names.get(self, "Unknown")


def _guess_from_tags(tags: list[str]) -> ChallengeCategory | None:
    """Guess category from challenge tags."""
    tag_text = " ".join(t.lower() for t in tags)
    for keyword in ("rev", "reverse", "reversing", "binary exploitation"):
        if keyword in tag_text:
            return ChallengeCategory.REV
    for keyword in ("pwn", "binary", "exploit", "rop", "shellcode", "bof"):
        if keyword in tag_text:
            return ChallengeCategory.PWN
    for keyword in ("web", "xss", "sqli", "csrf", "ssrf", "lfi"):
        if keyword in tag_text:
            return ChallengeCategory.WEB
    for keyword in ("crypto", "cipher", "encrypt", "rsa", "aes", "hash", "xor"):
        if keyword in tag_text:
            return ChallengeCategory.CRYPTO
    for keyword in ("forensic", "foren", "memory", "disk", "pcap", "capture"):
        if keyword in tag_text:
            return ChallengeCategory.FORENSICS
    for keyword in ("osint", "recon", "investigation"):
        if keyword in tag_text:
            return ChallengeCategory.OSINT
    return None


def _guess_from_files(filenames: list[str]) -> ChallengeCategory | None:
    """Guess category from attached file extensions."""
    ext_map: list[tuple[set[str], ChallengeCategory]] = [
        ({".elf", ".exe", ".dll", ".so", ".bin", ".o", ".class", ".jar", ".apk",
          ".wasm", ".ko", ".sys"}, ChallengeCategory.REV),
        ({".elf", ".exe", ".core", ".dump"}, ChallengeCategory.PWN),
        ({".html", ".php", ".js", ".ts", ".jsx", ".vue", ".sql", ".asp", ".aspx",
          ".jsp"}, ChallengeCategory.WEB),
        ({".pem", ".key", ".enc", ".encrypted", ".cipher", ".enc", ".pgp", ".gpg",
          ".sig", ".cert", ".crl"}, ChallengeCategory.CRYPTO),
        ({".pcap", ".pcapng", ".raw", ".dmp", ".mem", ".vmem", ".e01",
          ".ad1", ".img", ".dd", ".iso"}, ChallengeCategory.FORENSICS),
        ({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".wav", ".mp3", ".mp4",
          ".avi", ".zip", ".7z", ".rar", ".tar", ".gz"}, ChallengeCategory.MISC),
    ]
    for ext in {Path(f).suffix.lower() for f in filenames if "." in f}:
        for exts, cat in ext_map:
            if ext in exts:
                return cat
    return None


def _guess_from_description(description: str) -> ChallengeCategory | None:
    """Guess category from description text."""
    desc = description.lower()
    rev_kw = ["reverse", "decompile", "disassemble", "crackme", "keygen",
              "license", "serial", "obfuscated", "anti-debug"]
    if any(k in desc for k in rev_kw):
        return ChallengeCategory.REV

    pwn_kw = ["buffer overflow", "stack overflow", "rop", "return-oriented",
              "shellcode", "nx disabled", "aslr bypass", "format string",
              "heap", "use-after-free", "uaf", "arbitrary write", "got overwrite"]
    if any(k in desc for k in pwn_kw):
        return ChallengeCategory.PWN

    web_kw = ["web", "xss", "sqli", "sql injection", "csrf", "ssrf",
              "template injection", "ssti", "lfi", "rfi", "jwt",
              "authentication bypass", "api"]
    if any(k in desc for k in web_kw):
        return ChallengeCategory.WEB

    crypto_kw = ["crypto", "encrypt", "decrypt", "cipher", "rsa", "aes",
                 "hash", "md5", "sha", "xor", "substitution", "vigenere",
                 "padding", "oracle", "ecc", "diffie", "hellman"]
    if any(k in desc for k in crypto_kw):
        return ChallengeCategory.CRYPTO

    forensic_kw = ["forensic", "memory dump", "disk image", "pcap", "network capture",
                   "recover", "deleted", "stego", "steganography", "hidden"]
    if any(k in desc for k in forensic_kw):
        return ChallengeCategory.FORENSICS

    return None


def classify_challenge(
    name: str,
    category: str = "",
    tags: list[str] | None = None,
    description: str = "",
    filenames: list[str] | None = None,
) -> ChallengeCategory:
    """Classify a challenge into a category using all available signals.

    Priority: explicit category > tags > file extensions > description.
    """
    tags = tags or []
    filenames = filenames or []

    # 1. Explicit category field
    if category:
        result = ChallengeCategory.from_str(category)
        if result != ChallengeCategory.UNKNOWN:
            return result

    # 2. Tags
    result = _guess_from_tags(tags)
    if result:
        return result

    # 3. File extensions
    result = _guess_from_files(filenames)
    if result:
        return result

    # 4. Description text
    result = _guess_from_description(description)
    if result:
        return result

    # 5. Name heuristics
    name_lower = name.lower()
    if any(k in name_lower for k in ("rev", "reverse", "crackme")):
        return ChallengeCategory.REV
    if any(k in name_lower for k in ("pwn", "bof", "overflow", "shellcode", "rop")):
        return ChallengeCategory.PWN
    if any(k in name_lower for k in ("web", "xss", "sqli")):
        return ChallengeCategory.WEB
    if any(k in name_lower for k in ("crypto", "cipher", "enc", "rsa", "xor")):
        return ChallengeCategory.CRYPTO
    if any(k in name_lower for k in ("forensic", "foren", "memory", "pcap")):
        return ChallengeCategory.FORENSICS
    if any(k in name_lower for k in ("osint", "recon")):
        return ChallengeCategory.OSINT

    return ChallengeCategory.UNKNOWN
