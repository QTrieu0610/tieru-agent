from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tieru.capsule import CapsuleError, CapsuleLimits, CapsuleService
from tieru.capsule.manifest import encode_json, read_verified, sha256
from tieru.capsule.security import safe_member_path, validate_archive
from tieru.config import Settings, load_settings
from tieru.db import connect
from tieru.fabric.availability import AvailabilityService
from tieru.fabric.candidates import CandidateRegistry


def service(tmp_path: Path, name: str = "home", **settings_kwargs):
    settings = Settings(home=tmp_path / name, **settings_kwargs)
    settings.ensure_home()
    conn = connect(settings.home)
    return CapsuleService(settings, conn), settings, conn


def rewrite(source: Path, target: Path, transform):
    with zipfile.ZipFile(source) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()}
    transform(files)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def test_portable_export_manifest_scopes_and_structural_exclusions(tmp_path):
    capsule, settings, conn = service(tmp_path, api_key="sk-secret-should-never-export")
    settings.telegram_token = "telegram-secret-should-never-export"
    (settings.home / "SOUL.md").write_text("Local persona", encoding="utf-8")
    (settings.home / "traces" / "private.jsonl").write_text("do not export", encoding="utf-8")
    conn.execute("INSERT INTO facts(subject,content,provenance) VALUES('user','likes tea','explicit')")
    conn.commit()
    target = tmp_path / "portable.tieru"

    result = capsule.export(target)
    manifest, files = read_verified(target)

    assert manifest["format"] == "tieru-capsule"
    assert manifest["format_version"] == 1
    assert set(manifest["included_scopes"]) == {
        "identity", "memory", "skills", "preferences", "trust", "fabric"
    }
    assert "replay/replay_runs.json" not in files
    assert not any("trace" in name or "state.db" in name for name in files)
    raw = target.read_bytes()
    assert b"sk-secret-should-never-export" not in raw
    assert b"telegram-secret-should-never-export" not in raw
    assert result["manifest"]["item_counts"]["facts"] == 1


def test_manifest_hashes_cover_every_payload(tmp_path):
    capsule, _, conn = service(tmp_path)
    target = tmp_path / "hashes.tieru"
    capsule.export(target)
    manifest, files = read_verified(target)
    declared = {entry["path"]: entry for entry in manifest["files"]}
    assert set(declared) == set(files)
    assert all(declared[name]["sha256"] == sha256(payload) for name, payload in files.items())
    conn.close()


def test_inspect_is_read_only_and_dry_run_has_no_mutation(tmp_path):
    source_service, _, source_conn = service(tmp_path, "a")
    source_conn.execute("INSERT INTO facts(subject,content) VALUES('me','portable')")
    source_conn.commit()
    target = tmp_path / "identity.tieru"
    source_service.export(target)
    destination, settings, conn = service(tmp_path, "b")
    before = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    assert destination.inspect(target)["integrity"] == "verified"
    plan = destination.plan_import(target)
    assert plan.valid and plan.creates["facts"] == 1
    assert destination.import_capsule(target, plan=plan, dry_run=True)["valid"]
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == before
    assert not (settings.home / "skills").exists()


def test_round_trip_memory_graph_skill_fabric_and_pending_trust(tmp_path, monkeypatch):
    fabric = {
        "local-gemma": {"provider": "ollama", "model": "gemma4:latest", "local": True},
        "cloud": {"provider": "openai", "model": "gpt-example", "local": False},
    }
    trust = {"capabilities": {"local_write": {"mode": "allow", "paths": [r"C:\\old-machine"]}}}
    source, a, a_conn = service(
        tmp_path, "a", fabric_enabled=True, fabric_routing_policy="local_only",
        fabric_models=fabric, trust_policy=trust,
    )
    (a.home / "SOUL.md").write_text("Portable soul", encoding="utf-8")
    skill_dir = a.home / "skills" / "tea-helper"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: tea-helper\ndescription: Brew tea carefully\n---\nUse warm water.\n",
        encoding="utf-8",
    )
    a_conn.execute("INSERT INTO facts(subject,content,provenance) VALUES('me','likes tea','user-said')")
    a_conn.execute("INSERT INTO episodes(happened_at,summary,provenance) VALUES('2026-01-01','Made tea','chat:1')")
    person = a_conn.execute(
        "INSERT INTO graph_entities(entity_type,canonical_name,normalized_name) VALUES('person','Me','me')"
    ).lastrowid
    project = a_conn.execute(
        "INSERT INTO graph_entities(entity_type,canonical_name,normalized_name) VALUES('project','Tea','tea')"
    ).lastrowid
    a_conn.execute(
        "INSERT INTO graph_relations(subject_id,predicate,object_id,source_type,source_ref) VALUES(?,?,?,?,?)",
        (person, "likes", project, "explicit_user_save", "fact:1"),
    )
    a_conn.commit()
    archive = tmp_path / "roundtrip.tieru"
    source.export(archive)

    destination, b, b_conn = service(tmp_path, "b")
    plan = destination.plan_import(archive)
    assert plan.valid and plan.trust_review_required
    assert plan.path_remaps
    result = destination.import_capsule(archive, plan=plan)

    fact = dict(b_conn.execute("SELECT * FROM facts").fetchone())
    assert fact["source"] == "capsule_import" and "user-said" in fact["provenance"]
    relation = dict(b_conn.execute("SELECT * FROM graph_relations").fetchone())
    assert relation["source_type"] == "capsule_import"
    assert "explicit_user_save" in relation["source_ref"]
    assert (b.home / "skills" / "tea-helper" / "SKILL.md").exists()
    assert (b.home / "SOUL.md").read_text(encoding="utf-8") == "Portable soul"
    assert not b.trust_policy
    assert (b.home / "capsule" / "pending-trust" / f"{plan.capsule_id}.json").exists()
    config = (b.home / "config.yaml").read_text(encoding="utf-8")
    assert "local_only" in config and "gpt-example" in config
    assert "api_key" not in config.lower()
    for key in ("OPENAI_API_KEY", "TIERU_API_KEY", "TIERU_MAIN_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    loaded = load_settings({"home": b.home, "config_path": b.home / "config.yaml"})
    assert loaded.fabric_routing_policy == "local_only"
    cloud = CandidateRegistry(loaded).get("cloud")
    status = AvailabilityService(loaded, probe=lambda _candidate: True).check(cloud)
    assert not status.available and status.reason == "unavailable_credentials"
    assert Path(result["backup"]).joinpath("state.db").exists()


def test_identical_skill_skips_and_conflict_keeps_existing(tmp_path):
    source, a, _ = service(tmp_path, "a")
    skill = a.home / "skills" / "portable-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    content = "---\nname: portable-skill\ndescription: A portable test skill\n---\nSafe.\n"
    skill.write_text(content, encoding="utf-8")
    archive = tmp_path / "skill.tieru"
    source.export(archive)
    destination, b, _ = service(tmp_path, "b")
    current = b.home / "skills" / "portable-skill" / "SKILL.md"
    current.parent.mkdir(parents=True)
    current.write_text(content, encoding="utf-8")
    assert destination.plan_import(archive).skips["skills"] == 1
    current.write_text(content + "different", encoding="utf-8")
    plan = destination.plan_import(archive)
    assert plan.conflicts["skills"][0]["action"] == "keep-existing"
    destination.import_capsule(archive, plan=plan)
    assert current.read_text(encoding="utf-8").endswith("different")


def test_optional_history_and_forge_remains_inactive(tmp_path):
    capsule, settings, conn = service(tmp_path, "a")
    conn.execute(
        "INSERT INTO replay_runs(id,started_at,status) VALUES('run_1','2026-01-01T00:00:00Z','completed')"
    )
    conn.execute(
        "INSERT INTO replay_events(id,run_id,sequence,timestamp,category,event_type) VALUES('event_1','run_1',0,'2026-01-01T00:00:00Z','loop','completed')"
    )
    conn.commit()
    draft = settings.home / "forge" / "drafts" / "draft_12345678"
    draft.mkdir(parents=True)
    draft.joinpath("SKILL.md").write_text(
        "---\nname: imported-draft\ndescription: An inactive imported draft\n---\nSafe workflow.\n",
        encoding="utf-8",
    )
    for name in ("metadata.json", "workflow.json", "evaluation.json"):
        draft.joinpath(name).write_text("{}", encoding="utf-8")
    target = tmp_path / "history.tieru"
    capsule.export(target, profile="full-local-history")
    manifest, files = read_verified(target)
    assert "replay" in manifest["included_scopes"]
    assert [row["sequence"] for row in json.loads(files["replay/replay_events.json"])] == [0]
    destination, b, _ = service(tmp_path, "b")
    plan = destination.plan_import(target)
    destination.import_capsule(target, plan=plan)
    assert (b.home / "forge" / "drafts" / "draft_12345678" / "SKILL.md").exists()
    assert not (b.home / "skills" / "draft_12345678").exists()


def test_corruption_and_missing_payload_rejected_without_mutation(tmp_path):
    capsule, _, _ = service(tmp_path, "a")
    good = tmp_path / "good.tieru"
    capsule.export(good)
    corrupt = tmp_path / "corrupt.tieru"
    rewrite(good, corrupt, lambda files: files.__setitem__("config/fabric.json", b"{}\n"))
    destination, _, conn = service(tmp_path, "b")
    plan = destination.plan_import(corrupt)
    assert not plan.valid and "integrity" in plan.errors[0]
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0

    missing = tmp_path / "missing.tieru"
    rewrite(good, missing, lambda files: files.pop("memory/facts.json"))
    assert not destination.plan_import(missing).valid


@pytest.mark.parametrize("name", ["../../outside.txt", "/absolute.txt", "C:/windows.txt"])
def test_malicious_paths_rejected(tmp_path, name):
    target = tmp_path / "evil.tieru"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr(name, "owned")
    with pytest.raises(CapsuleError):
        validate_archive(target, CapsuleLimits())
    assert not (tmp_path.parent / "outside.txt").exists()


def test_backslash_archive_path_is_rejected():
    with pytest.raises(CapsuleError, match="unsafe archive path"):
        safe_member_path(r"dir\windows.txt")


def test_duplicate_normalized_path_and_bounds_rejected(tmp_path):
    duplicate = tmp_path / "duplicate.tieru"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("A.json", "1")
        archive.writestr("a.json", "2")
    with pytest.raises(CapsuleError, match="duplicate"):
        validate_archive(duplicate, CapsuleLimits())
    with pytest.raises(CapsuleError, match="file count"):
        validate_archive(duplicate, CapsuleLimits(max_files=1))

    large = tmp_path / "large.tieru"
    with zipfile.ZipFile(large, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", "x" * 10_000)
    with pytest.raises(CapsuleError):
        validate_archive(large, CapsuleLimits(max_file_bytes=20_000, max_compression_ratio=2))


def test_duplicate_manifest_path_bad_hash_and_future_version_rejected(tmp_path):
    capsule, _, _ = service(tmp_path)
    good = tmp_path / "good.tieru"
    capsule.export(good)
    bad = tmp_path / "bad.tieru"

    def duplicate(files):
        manifest = json.loads(files["manifest.json"])
        manifest["files"].append(dict(manifest["files"][0]))
        files["manifest.json"] = encode_json(manifest)

    rewrite(good, bad, duplicate)
    with pytest.raises(CapsuleError, match="duplicate manifest"):
        read_verified(bad)

    future = tmp_path / "future.tieru"
    def bump(files):
        manifest = json.loads(files["manifest.json"])
        manifest["format_version"] = 2
        files["manifest.json"] = encode_json(manifest)
    rewrite(good, future, bump)
    with pytest.raises(CapsuleError, match="unsupported Capsule format version"):
        read_verified(future)


def test_secret_in_user_skill_blocks_export(tmp_path):
    capsule, settings, _ = service(tmp_path)
    skill = settings.home / "skills" / "bad-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: bad-skill\ndescription: unsafe\n---\nUse sk-1234567890abcdefghijkl.\n",
        encoding="utf-8",
    )
    with pytest.raises(CapsuleError, match="secret"):
        capsule.export(tmp_path / "secret.tieru")


def test_replay_private_reasoning_is_never_exported(tmp_path):
    capsule, _, conn = service(tmp_path)
    conn.execute(
        "INSERT INTO replay_runs(id,started_at,status) VALUES('run_private','2026-01-01','completed')"
    )
    conn.execute(
        """INSERT INTO replay_events
           (id,run_id,sequence,timestamp,category,event_type,payload_json)
           VALUES('event_private','run_private',0,'2026-01-01','model','completed',?)""",
        (json.dumps({"reasoning": "hidden chain of thought"}),),
    )
    conn.commit()
    with pytest.raises(CapsuleError, match="private reasoning"):
        capsule.export(tmp_path / "private.tieru", include=("replay",))


def test_simulated_import_failure_rolls_back(tmp_path, monkeypatch):
    source, a, a_conn = service(tmp_path, "a")
    (a.home / "SOUL.md").write_text("new soul", encoding="utf-8")
    a_conn.execute("INSERT INTO facts(subject,content) VALUES('me','new')")
    a_conn.commit()
    archive = tmp_path / "atomic.tieru"
    source.export(archive)
    destination, b, b_conn = service(tmp_path, "b")
    plan = destination.plan_import(archive)

    def fail(*_args):
        b_conn.execute("INSERT INTO facts(subject,content) VALUES('partial','bad')")
        raise RuntimeError("simulated")

    monkeypatch.setattr(destination, "_import_db", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        destination.import_capsule(archive, plan=plan)
    assert b_conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert not (b.home / "SOUL.md").exists()
