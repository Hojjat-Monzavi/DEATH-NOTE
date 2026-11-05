#!/usr/bin/env python3
"""
obsidian_png_to_jpg.py

Recursively scan an Obsidian vault for PNG images referenced in .md files,
convert them to compact JPGs, update references, and remove orphans.

Run with --dry-run first, and back up your vault.

Usage:
    python obsidian_png_to_jpg.py /path/to/vault [options]

Options:
    --dry-run           Show what would happen, change nothing
    --quality N         JPEG quality (default 85)
    --max-size N        Max width/height in px; larger images are downscaled
                        (default 1920; use 0 to disable)
    --trash-dir NAME    Move orphan PNGs to this subfolder instead of
                        deleting (default: "_orphans")
    --delete-orphans    Delete orphans instead of moving them to trash-dir
    --skip-orphans      Leave orphan PNGs alone

Requires: Pillow   (pip install Pillow)
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

try:
    from PIL import Image
except ImportError:
    sys.exit("Error: Pillow is required. Install with: pip install Pillow")


# ---------- Patterns -------------------------------------------------------

PNG_RE = re.compile(r'\.png$', re.IGNORECASE)

# Wikilink embed or link:  ![[file.png]]  [[file.png]]  ![[file.png|300]] etc.
WIKI_RE = re.compile(
    r'(!?\[\[)([^\]\|#\n]+?\.png)([^\]]*?)(\]\])',
    re.IGNORECASE,
)

# Markdown embed:  ![alt](path.png)  ![alt](path.png "title")  ![alt](<a b.png>)
MD_RE = re.compile(
    r'(!\[[^\]]*\]\(\s*)(<[^>]+?\.png>|[^)\s]+?\.png)(\s+"[^"]*")?(\s*\))',
    re.IGNORECASE,
)

DEFAULT_SKIP_DIRS = {'.git', '.obsidian', '.trash'}


# ---------- Discovery ------------------------------------------------------

def iter_files(root, name_re, skip_dirs):
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        if name_re.search(p.name):
            yield p


# ---------- Index & resolution --------------------------------------------

def build_index(pngs, vault):
    by_rel, by_name = {}, defaultdict(list)
    for p in pngs:
        by_rel[p.relative_to(vault).as_posix().lower()] = p
        by_name[p.name.lower()].append(p)
    return by_rel, by_name


def resolve_ref(ref, md_file, vault, by_rel, by_name):
    """Return the PNG Path the reference points to, or None."""
    ref = unquote(ref.strip())
    if ref.startswith('<') and ref.endswith('>'):
        ref = ref[1:-1]
    ref = ref.replace('\\', '/')
    key = ref.lower()

    # 1. Exact path relative to vault root
    if key in by_rel:
        return by_rel[key]

    # 2. Path relative to the markdown file
    try:
        rel = (md_file.parent / ref).resolve().relative_to(vault)
        k2 = rel.as_posix().lower()
        if k2 in by_rel:
            return by_rel[k2]
    except ValueError:
        pass

    # 3. Unique basename (Obsidian's "shortest path" wikilink style)
    base = Path(ref).name.lower()
    candidates = by_name.get(base, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        for c in candidates:
            if c.relative_to(vault).as_posix().lower().endswith(key):
                return c
        print(f"    WARN: ambiguous '{ref}' in {md_file.name}")
    return None


# ---------- Conversion -----------------------------------------------------

def swap_ext(ref):
    """Replace '.png' with '.jpg' in a reference string, keeping <...> intact."""
    if ref.startswith('<') and ref.endswith('>'):
        return '<' + re.sub(r'\.png$', '.jpg', ref[1:-1], flags=re.I) + '>'
    return re.sub(r'\.png$', '.jpg', ref, flags=re.I)


def convert_png(png_path, jpg_path, quality, max_size):
    with Image.open(png_path) as img:
        if max_size and (img.width > max_size or img.height > max_size):
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        if img.mode == 'P':
            img = img.convert('RGBA')
        if img.mode in ('RGBA', 'LA'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        jpg_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(jpg_path, 'JPEG', quality=quality, optimize=True)


# ---------- Main -----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('vault', type=Path, help='Path to Obsidian vault root')
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what would happen, change nothing')
    ap.add_argument('--quality', type=int, default=85,
                    help='JPEG quality (default 85)')
    ap.add_argument('--max-size', type=int, default=1920,
                    help='Max width/height in px; 0 to disable (default 1920)')
    ap.add_argument('--trash-dir', default='_orphans',
                    help='Move orphans here instead of deleting (default "_orphans")')
    ap.add_argument('--delete-orphans', action='store_true',
                    help='Delete orphans instead of moving to trash-dir')
    ap.add_argument('--skip-orphans', action='store_true',
                    help='Leave orphan PNGs alone')
    args = ap.parse_args()

    vault = args.vault.resolve()
    if not vault.is_dir():
        sys.exit(f"Not a directory: {vault}")
    if args.max_size <= 0:
        args.max_size = None

    skip_dirs = set(DEFAULT_SKIP_DIRS)
    if not args.delete_orphans:
        skip_dirs.add(args.trash_dir)

    print(f"Vault: {vault}")
    pngs = list(iter_files(vault, PNG_RE, skip_dirs))
    mds  = list(iter_files(vault, re.compile(r'\.md$', re.I), skip_dirs))
    print(f"Found {len(pngs)} PNGs, {len(mds)} markdown files")
    if not pngs:
        return

    by_rel, by_name = build_index(pngs, vault)

    # ---------- Pass 1: find all references ----------
    refs, used, broken = [], set(), 0
    for md in mds:
        try:
            text = md.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            print(f"  SKIP (not UTF-8): {md.relative_to(vault)}")
            continue
        for m in WIKI_RE.finditer(text):
            png = resolve_ref(m.group(2), md, vault, by_rel, by_name)
            if png:
                used.add(png)
                refs.append((md, m.start(), m.end(), m.groups(), 'wiki', png))
            else:
                broken += 1
                print(f"  Broken wikilink: '{m.group(2)}' in {md.relative_to(vault)}")
        for m in MD_RE.finditer(text):
            png = resolve_ref(m.group(2), md, vault, by_rel, by_name)
            if png:
                used.add(png)
                refs.append((md, m.start(), m.end(), m.groups(), 'md', png))
            else:
                broken += 1
                print(f"  Broken md link: '{m.group(2)}' in {md.relative_to(vault)}")

    print(f"Resolved {len(refs)} references -> {len(used)} unique PNGs "
          f"({broken} unresolved)")

    # ---------- Pass 2: convert used PNGs to JPG ----------
    print("\n--- Converting ---")
    converted = {}
    for png in sorted(used):
        rel = png.relative_to(vault)
        jpg = png.with_suffix('.jpg')
        if jpg.exists():
            print(f"  SKIP (JPG already exists): {rel}")
            continue
        if not png.exists():
            print(f"  SKIP (missing): {rel}")
            continue
        old_size = png.stat().st_size
        if args.dry_run:
            print(f"  WOULD convert: {rel} ({old_size//1024} KB)")
            converted[png] = jpg
            continue
        try:
            convert_png(png, jpg, args.quality, args.max_size)
            new_size = jpg.stat().st_size
            pct = (1 - new_size / old_size) * 100 if old_size else 0
            print(f"  Converted: {rel} ({old_size//1024} -> {new_size//1024} KB, -{pct:.0f}%)")
            converted[png] = jpg
        except Exception as e:
            print(f"  ERROR converting {rel}: {e}")

    # ---------- Pass 3: rewrite references ----------
    print("\n--- Updating references ---")
    by_md = defaultdict(list)
    for r in refs:
        by_md[r[0]].append(r)

    for md, md_refs in by_md.items():
        text = md.read_text(encoding='utf-8')
        md_refs.sort(key=lambda r: r[1], reverse=True)  # end -> start
        changed = False
        for _, start, end, groups, kind, png in md_refs:
            if png not in converted:
                continue
            p1, p2, p3, p4 = groups
            if kind == 'wiki':
                new = p1 + swap_ext(p2) + p3 + p4
            else:
                new = p1 + swap_ext(p2) + (p3 or '') + p4
            if new != text[start:end]:
                text = text[:start] + new + text[end:]
                changed = True
        if changed:
            if args.dry_run:
                print(f"  WOULD update: {md.relative_to(vault)}")
            else:
                md.write_text(text, encoding='utf-8')
                print(f"  Updated: {md.relative_to(vault)}")

    # ---------- Pass 4: delete originals ----------
    print("\n--- Removing converted PNGs ---")
    for png in converted:
        if png.exists():
            if args.dry_run:
                print(f"  WOULD delete: {png.relative_to(vault)}")
            else:
                png.unlink()
                print(f"  Deleted: {png.relative_to(vault)}")

    # ---------- Pass 5: handle orphans ----------
    if args.skip_orphans:
        return
    orphans = [p for p in pngs if p not in used and p.exists()]
    if not orphans:
        return

    print(f"\n--- Handling {len(orphans)} orphan PNGs ---")
    trash = None
    if not args.delete_orphans:
        trash = vault / args.trash_dir
        if not args.dry_run:
            trash.mkdir(parents=True, exist_ok=True)

    for png in orphans:
        rel = png.relative_to(vault)
        if args.dry_run:
            verb = 'delete' if args.delete_orphans else f'move to {args.trash_dir}/'
            print(f"  WOULD {verb}: {rel}")
        elif args.delete_orphans:
            png.unlink()
            print(f"  Deleted orphan: {rel}")
        else:
            dest, i = trash / rel.name, 1
            while dest.exists():
                dest = trash / f"{rel.stem}_{i}{rel.suffix}"
                i += 1
            png.rename(dest)
            print(f"  Moved orphan: {rel} -> {args.trash_dir}/{dest.name}")

    print("\nDone.")


if __name__ == '__main__':
    main()