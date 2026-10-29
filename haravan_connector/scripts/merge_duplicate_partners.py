# -*- coding: utf-8 -*-
"""
Merge duplicate res.partner records created by the Haravan sync.

Why duplicates exist
--------------------
- haravan.customer._upsert_customer() matches an existing partner by an *exact*
  email/phone string before creating one. When Haravan sends a phone in a
  different format than the one stored by _onchange_phone_validation
  ("+8490..." vs "090..."), the lookup misses and a NEW partner is created,
  even though haravan_customer_id is the same.
- haravan.order._get_or_create_partner() creates a fresh "Haravan Guest" every
  time email AND phone are empty.

Dedup strategy (per user decision)
----------------------------------
Two partners are considered the same person if they share EITHER:
  1. the same non-empty haravan_customer_id, OR
  2. the same normalized phone (E.164 / VN national, from `phone` or `mobile`)
Partners are clustered with union-find so a record linked through either key
ends up in one group.

Master (kept) record per cluster:
  prefer one that HAS haravan_customer_id, then the smallest id (oldest).
All other records in the cluster are merged into the master using the standard
Odoo merge wizard (base.partner.merge.automatic.wizard._merge), which safely
re-points every foreign key (sale orders, invoices, mappings, ...).

How to run
----------
Dry-run (default, NO writes) — prints the report only:
    docker compose exec -T odoo /entrypoint.sh odoo shell -d <DB> --no-http \
        < src/dev_addons/haravan_connector/scripts/merge_duplicate_partners.py

Execute the merge for real — set DO_MERGE=1 in the environment:
    docker compose exec -T -e DO_MERGE=1 odoo /entrypoint.sh odoo shell -d <DB> \
        --no-http < src/dev_addons/haravan_connector/scripts/merge_duplicate_partners.py

Test DB first:  -d yuthfarm_db_test_quytrinh   (never production unless told).
The script commits only when DO_MERGE=1; in dry-run it rolls back.
"""
import logging
import os
import re

_logger = logging.getLogger("merge_duplicate_partners")

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
# Helpers
# --------------------------------------------------------------------------- #
def normalize_phone(raw):
    """Return a canonical phone key, or '' when nothing usable.

    Uses the phonenumbers lib when available (region VN); otherwise falls back
    to a digit-only normalization that folds +84 / 84 / leading-0 forms.
    """
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


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def pick_master(partners):
    """Master = has haravan_customer_id first, then smallest id."""
    return sorted(
        partners,
        key=lambda p: (0 if p.haravan_customer_id else 1, p.id),
    )[0]


def order_count(partner):
    return env["sale.order"].sudo().search_count([("partner_id", "=", partner.id)])


# --------------------------------------------------------------------------- #
# Build candidate set + clusters
# --------------------------------------------------------------------------- #
Partner = env["res.partner"].sudo()

# Only top-level individual contacts that look like Haravan customers.
# Excludes companies, child addresses (delivery/invoice) and internal users.
candidates = Partner.search(
    [
        ("is_company", "=", False),
        ("type", "=", "contact"),
        ("parent_id", "=", False),
        "|",
        ("haravan_customer_id", "!=", False),
        ("customer_rank", ">", 0),
    ]
)
candidates = candidates.filtered(lambda p: not p.user_ids)

_logger.info("Scanning %s candidate partners", len(candidates))

uf = UnionFind()
by_hid = {}
by_phone = {}
for p in candidates:
    uf.find(p.id)  # register
    hid = (p.haravan_customer_id or "").strip()
    if hid:
        by_hid.setdefault(hid, []).append(p.id)
    key = normalize_phone(p.phone)
    if not key and HAS_MOBILE:
        key = normalize_phone(p.mobile)
    if key:
        by_phone.setdefault(key, []).append(p.id)

for ids in list(by_hid.values()) + list(by_phone.values()):
    for other in ids[1:]:
        uf.union(ids[0], other)

clusters = {}
id2partner = {p.id: p for p in candidates}
for pid in id2partner:
    clusters.setdefault(uf.find(pid), []).append(pid)

dup_clusters = [ids for ids in clusters.values() if len(ids) > 1]
dup_clusters.sort(key=len, reverse=True)

total_dups = sum(len(ids) - 1 for ids in dup_clusters)
print("=" * 78)
print(f"Mode            : {'EXECUTE MERGE' if DO_MERGE else 'DRY-RUN (no writes)'}")
print(f"Candidates      : {len(candidates)}")
print(f"Duplicate groups: {len(dup_clusters)}")
print(f"Records to merge: {total_dups}  (will be removed, data moved to master)")
print("=" * 78)


# --------------------------------------------------------------------------- #
# Report + (optionally) merge
# --------------------------------------------------------------------------- #
Wizard = env["base.partner.merge.automatic.wizard"].sudo()
merged_ok = 0
failed = 0

for ids in dup_clusters:
    partners = id2partner[ids[0]].browse(ids)
    master = pick_master(partners)
    others = partners - master

    print(f"\n[group] {len(partners)} records -> keep id={master.id}")
    for p in partners:
        flag = "MASTER" if p == master else "merge "
        print(
            f"   {flag} id={p.id:<7} hid={p.haravan_customer_id or '-':<12} "
            f"phone={(p.phone or '-'):<16} email={(p.email or '-'):<28} "
            f"orders={order_count(p)}  name={p.name!r}"
        )

    if not DO_MERGE:
        continue

    # _merge accepts at most 3 contacts at once -> fold others in pairs.
    remaining = list(others)
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
