"""Read injected, preapproved source snapshots; never discover filesystem paths."""
from typing import Protocol
from ..domain.joint_feasibility import json_value, seal
from ..domain.existing_site_optimization import proposal_flags


class SourceSnapshotReader(Protocol):
    def read_snapshot(self, *, source_spec: dict) -> dict: ...


def discover_existing_domain(*, discovery_scope: dict, source_manifest: list[dict],
                             reader: SourceSnapshotReader) -> dict:
    scope, manifest = json_value([discovery_scope, source_manifest])
    snapshots, ledger, unknown, sites, aliases = [], [], [], {}, {}
    ids = [s.get("source_id") for s in manifest]
    if any(not isinstance(s, str) or not s for s in ids) or len(set(ids)) != len(ids):
        raise ValueError("source identity missing or duplicated")
    inconsistent = False
    pending = []
    for spec in manifest:
        source_id = spec["source_id"]
        try:
            snapshot = json_value(reader.read_snapshot(source_spec=json_value(spec)))
        except (FileNotFoundError, PermissionError, OSError) as exc:
            ledger.append(dict(source_id=source_id, reason="missing_source", error=type(exc).__name__))
            snapshots.append(dict(source_spec=spec, raw_count=None, status="missing_source"))
            continue
        records = snapshot.get("records")
        if not isinstance(records, list):
            raise ValueError("snapshot records must be a list")
        page = snapshot.get("pagination") or {}
        expected = spec.get("expected_historical_count")
        summaries = dict(source_spec=spec, source_id=source_id, raw_count=len(records),
                         expected_historical_count=expected, snapshot=snapshot)
        snapshots.append(summaries)
        if expected is not None and expected != len(records):
            ledger.append(dict(source_id=source_id, reason="historical_count_mismatch", actual=len(records), expected=expected))
        pages = page.get("pages_read", [])
        total = page.get("expected_pages")
        if (page.get("complete") is not True or not isinstance(total, int) or isinstance(total, bool)
                or total < 1 or set(pages) != set(range(1, total + 1)) or len(pages) != total):
            ledger.append(dict(source_id=source_id, reason="missing_page", pagination=page))
        for field in ("crs", "vertical_datum", "source_version"):
            if not snapshot.get(field):
                ledger.append(dict(source_id=source_id, reason="missing_" + field))
        seen = {}
        for index, row in enumerate(records):
            if not isinstance(row, dict): raise ValueError("source record must be an object")
            rid = row.get("raw_id")
            entry = dict(source_id=source_id, row_index=index, record=row)
            reason = None
            if not isinstance(rid, str) or not rid: reason = "missing_raw_id"
            key = source_id + ":" + str(rid)
            if key in seen:
                reason = "duplicate_raw_identity"
                inconsistent = inconsistent or seen[key] != row
            seen[key] = row
            coordinates = row.get("coordinates")
            valid = (isinstance(coordinates, list) and len(coordinates) == 2 and
                     all(type(x) in (int, float) for x in coordinates))
            if valid and snapshot.get("crs") == "EPSG:4326":
                valid = -180 <= coordinates[0] <= 180 and -90 <= coordinates[1] <= 90
            if not valid and reason is None: reason = "invalid_coordinates"
            if row.get("out_of_scope") is True:
                if row.get("scope_source_ref"):
                    ledger.append(dict(**entry, reason="out_of_scope", disposition="excluded")); continue
                reason = reason or "scope_evidence_missing"
            if row.get("new_build") is True:
                ledger.append(dict(**entry, reason="new_build_out_of_scope", disposition="excluded")); continue
            kind = spec.get("source_kind")
            legal = row.get("legal_host_evidence") or {}
            if kind not in ("tower", "existing_cns", "registered_existing_host") and not (
                    legal.get("status") == "confirmed" and legal.get("source_ref")):
                reason = reason or "legal_host_evidence_missing"
            if reason:
                unknown.append(dict(**entry, reason=reason))
                continue
            existence = row.get("existing_evidence") or {}
            if existence.get("status") != "confirmed" or not existence.get("source_ref"):
                unknown.append(dict(**entry, reason="existing_evidence_missing"))
                continue
            aliases[key] = key
            sites[key] = dict(row, physical_site_id=key, source_records=[dict(source_id=source_id, raw_id=rid)],
                              source_kind=kind, crs=snapshot.get("crs"), vertical_datum=snapshot.get("vertical_datum"))
            if row.get("alias_of"):
                target = row["alias_of"]
                if ":" not in target: target = source_id + ":" + target
                if row.get("alias_source_ref"): pending.append((key, target, row["alias_source_ref"]))
                else: ledger.append(dict(**entry, reason="alias_evidence_missing"))
    links = {}
    for key, target, ref in pending:
        if target not in aliases:
            unknown.append(dict(raw_key=key, reason="alias_target_missing", target=target, source_ref=ref))
        elif key == target:
            inconsistent = True
            ledger.append(dict(raw_key=key, reason="alias_identity_conflict"))
        else: links[key] = target
    for key in list(aliases):
        chain = set(); target = key
        while target in links and target not in chain:
            chain.add(target); target = links[target]
        if target in chain:
            inconsistent = True
            ledger.append(dict(raw_key=key, reason="alias_identity_conflict")); continue
        aliases[key] = target
    for key, target in aliases.items():
        if key != target:
            sites[target]["source_records"].extend(sites[key]["source_records"])
    physical = [dict(v, aliases=sorted(k for k, target in aliases.items() if target == sid and k != sid))
                for sid, v in sorted(sites.items()) if aliases[sid] == sid]
    status = "input_inconsistent" if inconsistent else "incomplete" if ledger or unknown or not manifest else "complete_within_supplied_manifest"
    return seal(dict(schema_version="existing_site_discovery.v1", discovery_scope=scope,
        source_manifest=manifest, source_snapshots=snapshots, raw_to_physical_aliases=aliases,
        physical_sites=physical, discovery_ledger=ledger, unknown_records=unknown,
        discovery_status=status, source_authentication_status="not_verified", **proposal_flags()), "discovery_fingerprint")


def review_domain_completeness(*, domain_review: dict,
                               independent_review_receipts: list[dict]) -> dict:
    from ..domain.existing_site_optimization import DIMENSIONS, SCHEMA_REVIEW, validate_proof_scope
    from ..domain.joint_feasibility import verify
    review, receipts = json_value([domain_review, independent_review_receipts])
    if review.get("schema_version") != SCHEMA_REVIEW: raise ValueError("domain review schema")
    frozen = review.get("frozen_domain") or {}
    verify(frozen, "domain_fingerprint")
    scope = validate_proof_scope(review.get("proof_scope") or {})
    dimensions, gaps = {}, []
    for name in DIMENSIONS:
        declaration = (review.get("dimensions") or {}).get(name) or {}
        dimensions[name] = dict(declared_status=declaration.get("status", "unknown"),
            evidence=declaration, verification_status="not_verified")
        gaps.append(dict(dimension=name, reason="independent_verification_unavailable"))
    # No trusted validator exists in the current contract. Receipts are retained
    # as references only, regardless of who supplied verified=true or a digest.
    gaps.extend(dict(dimension=dimension, reason=reason) for dimension,reason in (
        ("required_constraints", "communication_1000m_unconsumed"),
        ("dependency_closure", "rtk_segment_evidence_missing"),
        ("family_coverage", "radar_actions_unsupported"),
        ("family_coverage", "endpoint_actions_unsupported")))
    if frozen.get("unknown_candidates"):
        gaps.append(dict(dimension="qualification",reason="unknown_candidates_retained"))
    return seal(dict(schema_version=SCHEMA_REVIEW, frozen_domain=frozen,
        original_domain_fingerprint=frozen["domain_fingerprint"],
        source_manifest=review.get("source_manifest",frozen.get("discovery_manifest")),
        proof_scope=scope, dimensions=dimensions, independent_review_receipts=receipts,
        real_domain_proof_status="not_verified", review_status="evidence_incomplete",
        requested_scope_status="blocked" if scope["kind"] in ("reviewed_real_existing_domain", "complete_required_cns") else "declared_scope_only",
        complete_required_cns_status="unknown", formal_ue_status="not_verified",
        independent_verification_status="not_verified", evidence_gaps=gaps,
        **proposal_flags()), "review_fingerprint")
