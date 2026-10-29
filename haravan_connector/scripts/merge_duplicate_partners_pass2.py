# -*- coding: utf-8 -*-
"""
Merge duplicate res.partner — PASS 2 (same phone + reversed-name / different type).

Why a second pass
-----------------
Pass 1 (merge_duplicate_partners.py) only clustered TOP-LEVEL INDIVIDUALS
(is_company=False). It left two kinds of duplicates untouched:

  1. Same phone but DIFFERENT type — one record is an individual, the other a
     company (same real customer, Haravan created the wrong type). Pass 1 filtered
     companies out entirely.
  2. Same phone but the NAME is reversed — e.g. "Nguyen Van A" vs "A Van Nguyen"
     (Haravan glued first_name/last_name in the opposite order).

Dedup rule (per user decision)
------------------------------
Two records (A, B) are merged when normalize_phone(A) == normalize_phone(B) AND
    A.is_company != B.is_company            # different type, OR
    name_tokens(A)[::-1] == name_tokens(B)  # reversed name

Master (kept) record:
    prefer the one WITHOUT haravan_customer_id (the human-entered original),
    then the one with the most sale orders, then the smallest id.
The standard Odoo merge wizard fills empty master fields from the merged record,
so haravan_customer_id is preserved on the master while its identity (name/type)
stays that of the original.

Merge mechanics = standard Odoo:
    base.partner.merge.automatic.wizard._merge() re-points every foreign key
    (sale.order, account.move, haravan.customer mapping, child addresses' parent_id,
    ...) onto the master, fills empty values, then unlinks the duplicate.

How to run
----------
Dry-run (default, NO writes) — prints the report only:
    docker compose exec -T odoo_uat /entrypoint.sh odoo shell -d <DB> --no-http \
        < src/dev_addons/haravan_connector/scripts/merge_duplicate_partners_pass2.py

Execute for real — set DO_MERGE=1:
    docker compose exec -T -e DO_MERGE=1 odoo_uat /entrypoint.sh odoo shell -d <DB> \
        --no-http < src/dev_addons/haravan_connector/scripts/merge_duplicate_partners_pass2.py

Backup first; merges are irreversible. The script commits only when DO_MERGE=1.
"""
import logging
import os
import re
import unicodedata

_logger = logging.getLogger("merge_duplicate_partners_pass2")

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

# `env` is injected by `odoo shell`. Guard so the file can also be imported.
try:
    env  # noqa: F821
except NameError:  # pragma: no cover - only when not run via odoo shell
    raise SystemExit("Run this file through `odoo shell` (env is undefined).")

DO_MERGE = os.environ.get("DO_MERGE", "").strip() in ("1", "true", "True", "yes")
DEFAULT_REGION = "VN"
# `mobile` was dropped from res.partner in Odoo 19; only read it if present.
HAS_MOBILE = "mobile" in env["res.partner"]._fields  # noqa: F821


# --------------------------------------------------------------------------- #
# Helpers (phone normalization identical to pass 1)
# --------------------------------------------------------------------------- #
def normalize_phone(raw):
    """Return a canonical phone key, or '' when nothing usable."""
    if not raw:
        return ""
    raw = raw.strip()
    if phonenumbers:
        try:
            num = phonenumbers.parse(raw, DEFAULT_REGION)
            if phonenumbers.is_valid_number(num):
                return phonenumbers.format_number(
                    num, phonenumbers.PhoneNumberFormat.E164
                )
        except phonenumbers.phonenumberutil.NumberParseException:
            pass
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if digits.startswith("0084"):
        digits = digits[4:]
    elif digits.startswith("84") and len(digits) >= 11:
        digits = digits[2:]
    digits = digits.lstrip("0")
    # Too short to be a real phone -> treat as no key (avoids false merges).
    if len(digits) < 8:
        return ""
    return "+84" + digits


def phone_key(partner):
    key = normalize_phone(partner.phone)
    if not key and HAS_MOBILE:
        key = normalize_phone(partner.mobile)
    return key


def _strip_accents(text):
    """Drop Vietnamese (and other) diacritics for accent-insensitive compare."""
    nfd = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def name_tokens(name):
    """Accent/case-insensitive whitespace tokens of a name (đ folded to d)."""
    cleaned = _strip_accents(name).lower().replace("đ", "d")
    return [t for t in cleaned.split() if t]


def is_reversed_name(name_a, name_b):
    """True when one name is the exact token-order reversal of the other."""
    ta, tb = name_tokens(name_a), name_tokens(name_b)
    return bool(ta) and bool(tb) and ta[::-1] == tb


def order_count(partner):
    return env["sale.order"].sudo().search_count([("partner_id", "=", partner.id)])


def should_merge(record, master):
    """Pass-2 rule, evaluated against the chosen master (phone already equal)."""
    return (
        record.is_company != master.is_company
        or is_reversed_name(record.name, master.name)
    )


def pick_master_pass2(partners):
    """Master = no haravan_customer_id first, then most orders, then smallest id."""
    return sorted(
        partners,
        key=lambda p: (1 if p.haravan_customer_id else 0, -order_count(p), p.id),
    )[0]


def merge_reason(record, master):
    reasons = []
    if record.is_company != master.is_company:
        reasons.append("diff-type")
    if is_reversed_name(record.name, master.name):
        reasons.append("reversed-name")
    return "+".join(reasons)


# --------------------------------------------------------------------------- #
# Build candidate set (INCLUDES companies — the key difference vs pass 1)
# --------------------------------------------------------------------------- #
Partner = env["res.partner"].sudo()

candidates = Partner.search(
    [
        ("type", "=", "contact"),
        ("parent_id", "=", False),
        "|",
        ("haravan_customer_id", "!=", False),
        ("customer_rank", ">", 0),
    ]
)
candidates = candidates.filtered(lambda p: not p.user_ids)

_logger.info("Scanning %s candidate partners", len(candidates))

# Group by normalized phone (only phones with >=2 records matter).
by_phone = {}
for p in candidates:
    key = phone_key(p)
    if key:
        by_phone.setdefault(key, []).append(p)

# Per phone group, pick a master and the records that satisfy the pass-2 rule.
# Everything is resolved here while all records are still alive; the merge phase
# below never re-scans `candidates` (it would hit already-unlinked ids).
groups = []  # list of (key, master, to_merge, skipped)
for key, members in by_phone.items():
    if len(members) < 2:
        continue
    group = candidates.browse([m.id for m in members])
    master = pick_master_pass2(group)
    to_merge = group.filtered(lambda r: r != master and should_merge(r, master))
    if to_merge:
        skipped = group - master - to_merge
        groups.append((key, master, to_merge, skipped))

groups.sort(key=lambda g: len(g[2]), reverse=True)
total_dups = sum(len(g[2]) for g in groups)

print("=" * 78)
print(f"Mode            : {'EXECUTE MERGE' if DO_MERGE else 'DRY-RUN (no writes)'}")
print(f"Candidates      : {len(candidates)}")
print(f"Mergeable groups: {len(groups)}")
print(f"Records to merge: {total_dups}  (will be removed, data moved to master)")
print("=" * 78)


def _fmt(partner, label, extra=""):
    return (
        f"   {label} id={partner.id:<7} "
        f"{'COMPANY' if partner.is_company else 'person ':<7} "
        f"hid={partner.haravan_customer_id or '-':<12} "
        f"phone={(partner.phone or '-'):<16} orders={order_count(partner):<4} "
        f"name={partner.name!r}{(' [' + extra + ']') if extra else ''}"
    )


# --------------------------------------------------------------------------- #
# Report + (optionally) merge
# --------------------------------------------------------------------------- #
Wizard = env["base.partner.merge.automatic.wizard"].sudo()
merged_ok = 0
failed = 0

for key, master, to_merge, skipped in groups:
    print(f"\n[group] phone={key} -> keep id={master.id}")
    print(_fmt(master, "MASTER"))
    for r in to_merge:
        warn = ""
        if r.haravan_customer_id and master.haravan_customer_id and \
                r.haravan_customer_id != master.haravan_customer_id:
            warn = "WARN both-have-different-haravan-id"
        print(_fmt(r, "merge ", merge_reason(r, master) + (f" {warn}" if warn else "")))
    for r in skipped:
        print(_fmt(r, "skip  ", "rule not matched"))

    if not DO_MERGE:
        continue

    # _merge accepts at most 3 contacts at once -> fold others in pairs.
    remaining = list(to_merge)
    while remaining:
        batch = remaining[:2]
        remaining = remaining[2:]
        batch_ids = [master.id] + [b.id for b in batch]
        try:
            with env.cr.savepoint():
                Wizard._merge(batch_ids, dst_partner=master, extra_checks=False)
            merged_ok += len(batch)
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            print(f"   !! merge failed for {[b.id for b in batch]}: {exc}")

print("\n" + "=" * 78)
if DO_MERGE:
    env.cr.commit()
    print(f"DONE. merged={merged_ok} failed={failed}. Changes COMMITTED.")
else:
    env.cr.rollback()
    print("DRY-RUN complete. No changes written. Set DO_MERGE=1 to execute.")
print("=" * 78)
