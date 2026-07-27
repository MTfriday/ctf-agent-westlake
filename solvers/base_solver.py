"""Base class for category-specific solvers — generates category-tuned prompts and tool lists."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from solvers.router import ChallengeCategory


@dataclass
class CategoryTool:
    """A category-specific tool that can be registered with the solver."""

    name: str
    description: str
    command_template: str  # bash command template, {path} is placeholder
    category: ChallengeCategory


# Registry of category-specific tools
CATEGORY_TOOLS: dict[ChallengeCategory, list[CategoryTool]] = {
    ChallengeCategory.REV: [
        CategoryTool("decompile", "Decompile a binary with Ghidra/pyghidra",
                     "python3 -c \"import pyghidra; ...\"", ChallengeCategory.REV),
        CategoryTool("strings_analysis", "Extract strings with filtering",
                     "strings -n 6 {path} | grep -iE 'flag|key|secret|password|http'", ChallengeCategory.REV),
        CategoryTool("angr_symbolic", "Run angr symbolic execution to find correct input",
                     "python3 -c \"import angr; ...\"", ChallengeCategory.REV),
        CategoryTool("objdump_analysis", "Disassemble binary sections",
                     "objdump -d {path} 2>/dev/null | head -200", ChallengeCategory.REV),
        CategoryTool("readelf_analysis", "Read ELF headers and symbols",
                     "readelf -a {path} 2>/dev/null | head -100", ChallengeCategory.REV),
        CategoryTool("r2_analysis", "Quick radare2 analysis",
                     "r2 -q -c 'aaa; afl~flag,main,win,test,check; quit' {path}", ChallengeCategory.REV),
    ],
    ChallengeCategory.PWN: [
        CategoryTool("checksec", "Check binary security protections",
                     "python3 -c \"from pwn import *; e=ELF('{path}'); print(e.checksec())\"",
                     ChallengeCategory.PWN),
        CategoryTool("rop_gadgets", "Find ROP gadgets in binary",
                     "ROPgadget --binary {path} 2>/dev/null | head -50", ChallengeCategory.PWN),
        CategoryTool("strings_analysis", "Find key strings and symbols",
                     "strings -n 5 {path} | grep -iE 'flag|win|shell|bin|system|exec|cats'", ChallengeCategory.PWN),
        CategoryTool("gdb_info", "Check binary info with GDB",
                     "file {path} && gdb -batch -ex 'info functions' {path} 2>/dev/null | head -40",
                     ChallengeCategory.PWN),
        CategoryTool("angr_find", "Use angr to find path to target address",
                     "python3 -c \"import angr; ...\"", ChallengeCategory.PWN),
    ],
    ChallengeCategory.WEB: [
        CategoryTool("curl_headers", "Check HTTP headers",
                     "curl -s -I '{url}' 2>/dev/null", ChallengeCategory.WEB),
        CategoryTool("curl_get", "Full HTTP GET request",
                     "curl -s -L -k '{url}' 2>/dev/null", ChallengeCategory.WEB),
        CategoryTool("curl_cookies", "Check cookies and response details",
                     "curl -s -v '{url}' 2>&1 | head -80", ChallengeCategory.WEB),
        CategoryTool("check_robots", "Check robots.txt",
                     "curl -s -k '{url}/robots.txt' 2>/dev/null", ChallengeCategory.WEB),
        CategoryTool("check_sitemap", "Check sitemap.xml",
                     "curl -s -k '{url}/sitemap.xml' 2>/dev/null", ChallengeCategory.WEB),
    ],
    ChallengeCategory.CRYPTO: [
        CategoryTool("rsactftool", "Run automated RSA attacks",
                     "python3 -m RsatCtfTool --publickey {path} --private 2>/dev/null", ChallengeCategory.CRYPTO),
        CategoryTool("sage_quick", "Quick SageMath computation",
                     "sage -c \"...\"", ChallengeCategory.CRYPTO),
        CategoryTool("z3_solve", "Z3 constraint solving",
                     "python3 -c \"from z3 import *; ...\"", ChallengeCategory.CRYPTO),
        CategoryTool("cipher_identify", "Identify cipher type",
                     "python3 -c \"import pycryptodome; ...\"", ChallengeCategory.CRYPTO),
    ],
    ChallengeCategory.MISC: [
        CategoryTool("zsteg_check", "Check PNG/BMP for LSB steganography",
                     "zsteg -a {path} 2>/dev/null", ChallengeCategory.MISC),
        CategoryTool("binwalk_extract", "Extract embedded files with binwalk",
                     "binwalk -e {path} -C /challenge/workspace/ 2>/dev/null", ChallengeCategory.MISC),
        CategoryTool("steghide_extract", "Extract data from JPEG/BMP/WAV",
                     "steghide extract -sf {path} -p '' -f 2>/dev/null", ChallengeCategory.MISC),
        CategoryTool("exiftool_read", "Read all metadata",
                     "exiftool {path} 2>/dev/null", ChallengeCategory.MISC),
        CategoryTool("strings_analysis", "Extract embedded strings from any file",
                     "strings -n 8 {path} 2>/dev/null | head -100", ChallengeCategory.MISC),
    ],
    ChallengeCategory.FORENSICS: [
        CategoryTool("mmls_info", "Check disk partition layout",
                     "mmls {path} 2>/dev/null", ChallengeCategory.FORENSICS),
        CategoryTool("fls_list", "List files (including deleted) from disk image",
                     "fls -r {path} 2>/dev/null | head -100", ChallengeCategory.FORENSICS),
        CategoryTool("foremost_carve", "Carve files from disk image",
                     "foremost -i {path} -o /challenge/workspace/foremost/ 2>/dev/null",
                     ChallengeCategory.FORENSICS),
        CategoryTool("binwalk_extract", "Extract embedded files",
                     "binwalk -e {path} -C /challenge/workspace/ 2>/dev/null", ChallengeCategory.FORENSICS),
        CategoryTool("strings_analysis", "Extract strings from image",
                     "strings -n 10 {path} 2>/dev/null | head -200", ChallengeCategory.FORENSICS),
    ],
    ChallengeCategory.OSINT: [
        CategoryTool("curl_get", "Fetch webpage content",
                     "curl -s -L -A 'Mozilla/5.0' '{url}' 2>/dev/null", ChallengeCategory.OSINT),
        CategoryTool("whois_query", "Lookup WHOIS information",
                     "whois '{target}' 2>/dev/null | head -40", ChallengeCategory.OSINT),
    ],
}


def get_category_prompt_suffix(category: ChallengeCategory) -> str:
    """Get category-specific strategy instructions appended to the system prompt."""
    prompts = {
        ChallengeCategory.REV: (
            "## Reverse Engineering Strategy\n"
            "- Run `file`, `strings`, `readelf -a` first to understand the binary\n"
            "- Use `pyghidra` for decompilation (import pyghidra; with pyghidra.open_program(...) as flat_api:)\n"
            "- Look for comparison functions, flag check routines, XOR/encryption loops\n"
            "- Try symbolic execution with angr if you need to find the correct input\n"
            "- Common patterns: hardcoded keys, byte-by-byte comparison, XOR cipher\n"
            "- Check for packed/obfuscated binaries with `binwalk` and `strings`"
        ),
        ChallengeCategory.PWN: (
            "## Binary Exploitation Strategy\n"
            "- First run `python3 -c \"from pwn import *; e=ELF('binary'); print(e.checksec())\"`\n"
            "- Check for win functions with `gdb -batch -ex 'info functions' binary`\n"
            "- Use `strings` to find interesting symbols (system, /bin/sh, win, flag)\n"
            "- Write pwntools scripts for interaction, not raw nc\n"
            "- For ROP: use ROPgadget to find gadgets, pwntools to build chain\n"
            "- For format string: use pwntools fmtstr_payload\n"
            "- For heap: use pwntools and understand the allocator\n"
            "- Run locally first, then connect to remote service"
        ),
        ChallengeCategory.WEB: (
            "## Web Security Strategy\n"
            "- Start with `curl -s -v <url>` to see full response headers\n"
            "- Check robots.txt, sitemap.xml, .git/config, backup files\n"
            "- Try common endpoints: /admin, /api, /flag, /.env\n"
            "- For SQLi: try sqlmap with `python3 -m sqlmap -u <url> --batch`\n"
            "- For XSS: use webhook_create() + webhook_get_requests() for exfiltration\n"
            "- For JWT: decode with `python3 -c \"import jwt; print(jwt.decode(token, options={'verify_signature': False}))\"`\n"
            "- For SSTI: try {{7*7}} in input fields\n"
            "- Check for IDOR by modifying parameters"
        ),
        ChallengeCategory.CRYPTO: (
            "## Cryptography Strategy\n"
            "- First identify: algorithm, key size, mode of operation\n"
            "- For RSA: check key size (small n → factor), check for Wiener, common modulus\n"
            "- Use RsaCtfTool for automated attacks\n"
            "- For XOR: find key length with hamming distance, frequency analysis\n"
            "- For AES: check for ECB mode (block swapping), nonce reuse\n"
            "- For hash: check online rainbow tables, length extension\n"
            "- Use z3 solver for constraint-based problems\n"
            "- SageMath for number theory operations"
        ),
        ChallengeCategory.MISC: (
            "## Miscellaneous Strategy\n"
            "- Check all attached files with appropriate tools\n"
            "- For images: zsteg (PNG), steghide (JPEG/BMP/WAV), strings, exiftool\n"
            "- For archives: extract with appropriate tool, examine contents\n"
            "- For audio: check spectrogram (sox), check for hidden tones\n"
            "- For PCAP: analyze with strings, tshark, or Python dpkt/scapy\n"
            "- For multi-layer files: use binwalk to extract embedded content\n"
            "- Think creatively — check file magic bytes, trailing data, comments"
        ),
        ChallengeCategory.FORENSICS: (
            "## Digital Forensics Strategy\n"
            "- For disk images: use mmls → fls → icat to navigate\n"
            "- Recover deleted files with foremost or tsk_recover\n"
            "- Extract strings from the raw image\n"
            "- For memory dumps: use volatility3\n"
            "- For PCAP: strings first, then tshark or Python analysis\n"
            "- Check for hidden partitions, unusual file signatures"
        ),
        ChallengeCategory.OSINT: (
            "## OSINT Strategy\n"
            "- Gather all available information from the challenge description\n"
            "- Use curl to fetch web pages and APIs\n"
            "- Look for social media, public records, DNS information\n"
            "- Cross-reference all findings"
        ),
    }
    return prompts.get(category, "")


def get_distfile_hints(category: ChallengeCategory, distfiles: list[str]) -> list[str]:
    """Get category-specific hints about how to analyze distfiles."""
    hints: list[str] = []
    for fname in distfiles:
        ext = Path(fname).suffix.lower()
        if category == ChallengeCategory.REV:
            if ext in (".elf", ".exe", ".bin", ".o", ".so", ".dll"):
                hints.append(f"  {fname}: binary — run `file /challenge/distfiles/{fname}`")
            elif ext in (".py", ".js", ".java", ".go"):
                hints.append(f"  {fname}: source code — read with `read_file` or `cat`")
        elif category == ChallengeCategory.PWN:
            if ext in (".elf", ".exe"):
                hints.append(f"  {fname}: exploit target — run checksec and analyze")
        elif category == ChallengeCategory.CRYPTO:
            if ext in (".pem", ".key", ".enc", ".txt"):
                hints.append(f"  {fname}: crypto data — read with `read_file` or `xxd`")
        elif category == ChallengeCategory.MISC:
            if ext in (".png", ".jpg", ".bmp", ".gif"):
                hints.append(f"  {fname}: image — run zsteg/exiftool/steghide")
            elif ext in (".wav", ".mp3"):
                hints.append(f"  {fname}: audio — check spectrogram, strings")
            elif ext in (".pcap", ".pcapng"):
                hints.append(f"  {fname}: packet capture — run strings, tshark, scapy")
    return hints
