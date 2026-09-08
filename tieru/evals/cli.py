"""CLI entry points for M21 reliability runs, reports, and baselines."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from tieru.evals.baseline import compare_baseline, load_json_artifact, save_baseline
from tieru.evals.corpus import default_corpus_paths, default_live_corpus_paths, load_corpus
from tieru.evals.judge import ExistingRefereeJudge
from tieru.evals.preflight import probe_provider
from tieru.evals.report import render_comparison, render_scorecard, run_json, write_result
from tieru.evals.runner import EvalRunner


def run_eval_cli(args, _settings=None) -> int:
    command = args.eval_command
    if command == "doctor":
        from tieru.config import Settings

        settings = _settings or Settings()
        probe = probe_provider(settings)
        if getattr(args, "json", False):
            print(json.dumps(probe.to_dict(), indent=2))
        else:
            print("Tieru Provider Pre-flight Diagnostic")
            print("-" * 38)
            print(f"Provider:            {probe.provider}")
            print(f"Model:               {probe.model}")
            print(f"Endpoint:            {probe.endpoint}")
            print(f"Status:              {probe.status}")
            print(f"Reachable:           {'YES' if probe.reachable else 'NO'}")
            print(f"Credentials:         {probe.credentials_status}")
            print(f"Usage Telemetry:     {'AVAILABLE' if probe.usage_telemetry else 'UNAVAILABLE'}")
            if probe.models_available:
                print(f"Installed Models:    {', '.join(probe.models_available)}")
            if probe.error:
                print(f"Error:               {probe.error}")
        return 0 if probe.status == "READY" else 1

    if command == "run":
        if args.live:
            default_paths = default_live_corpus_paths() or default_corpus_paths()
        else:
            default_paths = default_corpus_paths()
        paths = tuple(Path(item) for item in (args.corpus or default_paths))
        corpus = load_corpus(paths).select(category=args.category, case_id=args.case_id)
        max_cases = getattr(args, "max_cases", None)
        if max_cases is not None:
            if not 1 <= int(max_cases) <= 500:
                raise ValueError("--max-cases must be between 1 and 500")
            if int(max_cases) < len(corpus.cases):
                corpus = replace(
                    corpus,
                    cases=corpus.cases[: int(max_cases)],
                    selection_scope="max_cases_subset",
                )
        timeout_seconds = getattr(args, "case_timeout_seconds", None)
        if timeout_seconds is not None:
            if not 1 <= int(timeout_seconds) <= 600:
                raise ValueError("--case-timeout-seconds must be between 1 and 600")
            corpus = replace(
                corpus,
                cases=tuple(
                    replace(case, timeout_ms=int(timeout_seconds) * 1000)
                    for case in corpus.cases
                ),
            )
        artifact_path = Path(args.output) if args.output else None
        if args.live and artifact_path is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            artifact_path = (
                Path(".tieru")
                / "evals"
                / "live"
                / f"live-{stamp}-{uuid4().hex[:8]}.json"
            )
        judge = ExistingRefereeJudge() if args.judge else None
        role_overrides: dict[str, str] | None = None
        if getattr(args, "role_model", None):
            role_overrides = {}
            for item in args.role_model:
                if "=" in item:
                    rk, rv = item.split("=", 1)
                    role_overrides[rk.strip()] = rv.strip()

        role_policy = None
        if getattr(args, "role_policy", None):
            from tieru.fabric.roles import load_role_policy

            role_policy = load_role_policy(args.role_policy)
            if role_policy is None:
                print(f"Error: Failed to load valid role policy from '{args.role_policy}'")
                return 1

        run = EvalRunner(
            judge=judge,
            live_settings=_settings if args.live else None,
            runs=getattr(args, "runs", 1),
            overall_timeout_seconds=getattr(args, "overall_timeout_seconds", 3600),
            provider_failure_threshold=getattr(args, "provider_failure_threshold", 3),
            artifact_path=artifact_path,
            role_overrides=role_overrides,
            role_policy=role_policy,
        ).run(corpus, mode="live" if args.live else "deterministic")
        if artifact_path is not None:
            write_result(run, artifact_path)
        print(run_json(run) if args.json else render_scorecard(run))
        if args.live and artifact_path is not None and not args.json:
            print(f"Live artifact: {artifact_path}")
        if args.compare:
            comparison = compare_baseline(load_json_artifact(args.compare), run)
            print(json.dumps({"comparison": {
                "passed": comparison.passed,
                "rows": list(comparison.rows),
                "regressions": list(comparison.regressions),
            }}, indent=2) if args.json else render_comparison(comparison))
            return 0 if run.reliability_pass and comparison.passed else 1
        return 0 if run.reliability_pass else 1
    if command == "roles":
        from tieru.config import Settings
        from tieru.evals.role_profiling import (
            build_baseline_artifact,
            discover_candidates,
            generate_recommendations,
            profile_all_roles,
        )
        from tieru.fabric.roles import ModelRole

        settings = _settings or Settings()
        candidates = discover_candidates(settings)
        if getattr(args, "model", None):
            candidates = [c for c in candidates if c.model == args.model]
            if not candidates:
                print(f"Error: Candidate model '{args.model}' not found among discovered candidates.")
                return 1

        selected_roles: list[ModelRole] | None = None
        if getattr(args, "role", None):
            try:
                selected_roles = [ModelRole(args.role.lower())]
            except ValueError:
                print(f"Error: Unknown cognitive role '{args.role}'. Valid: {', '.join(r.value for r in ModelRole)}")
                return 1

        runs = getattr(args, "runs", 1)
        profiles = profile_all_roles(settings, candidates, roles=selected_roles, runs=runs)
        baseline_model = settings.role("main").model
        recs = generate_recommendations(profiles, baseline_model)
        baseline_artifact = build_baseline_artifact(settings, profiles, recs, candidates)

        if getattr(args, "save_baseline", None):
            save_path = Path(args.save_baseline)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(json.dumps(baseline_artifact.to_dict(), indent=2), encoding="utf-8")
            if not getattr(args, "json", False):
                print(f"Saved role capability baseline to {save_path}")

        if getattr(args, "json", False):
            print(json.dumps(baseline_artifact.to_dict(), indent=2))
        else:
            print("Tieru Cognitive Role Capability Profiling (M33)")
            print("=" * 60)
            for prof in profiles:
                status_str = "PASS" if prof.safety_gate_passed else "REJECTED"
                print(f"Role: {prof.role:16} Model: {prof.model:20} Success: {prof.success_rate * 100:.1f}%  Safety: {status_str}  Avg Lat: {prof.average_latency_seconds:.2f}s")
            print("\nAdvisory Recommendations:")
            print("-" * 60)
            for rec in recs:
                print(f"Role: {rec.role:16} Recommendation: {rec.recommendation.value} ({rec.confidence.value}) -> {rec.recommended_model}")
                print(f"  Reason: {rec.reason}")
        return 0
    if command == "report":
        value = load_json_artifact(args.result)
        print(render_scorecard(value))
        return 0 if value.get("reliability_pass") else 1
    if command == "compare":
        comparison = compare_baseline(
            load_json_artifact(args.baseline), load_json_artifact(args.current)
        )
        print(render_comparison(comparison))
        return 0 if comparison.passed else 1
    if command == "baseline" and args.baseline_command == "save":
        save_baseline(load_json_artifact(args.result), args.path, is_live=getattr(args, "live", False))
        print(f"Saved versioned baseline to {args.path}")
        return 0
    if command == "trace":
        from tieru.config import Settings
        from tieru.db import connect
        from tieru.tasks.store import TaskStore

        settings = _settings or Settings()
        db_arg = getattr(args, "db", None)
        if db_arg:
            db_p = Path(db_arg)
            if db_p.is_file() or str(db_arg).endswith(".db"):
                home = db_p.parent
                db_file = db_p
            else:
                home = db_p
                db_file = home / "state.db"
        else:
            home = Path(getattr(settings, "home", None) or ".tieru")
            db_file = home / "state.db"
        if not db_file.exists():
            print(f"Database '{db_file}' does not exist.")
            return 1
        conn = connect(home)
        store = TaskStore(conn)
        try:
            task = store.get_task(args.task_id)
        except KeyError:
            task = None
        if not task:
            print(f"Task '{args.task_id}' not found.")
            return 1
        steps = store.list_steps(args.task_id)
        checkpoints = store.list_checkpoints(args.task_id)

        if getattr(args, "json", False):
            out_data = {
                "task": {
                    "task_id": task.task_id,
                    "goal": task.goal,
                    "status": task.status.value,
                },
                "steps": [
                    {
                        "step_id": s.step_id,
                        "position": s.position,
                        "title": s.title,
                        "status": s.status.value,
                        "verification_status": s.verification_status.value if s.verification_status else None,
                        "verification_summary": s.verification_summary,
                    }
                    for s in steps
                ],
                "checkpoints": [cp.public() for cp in checkpoints],
            }
            print(json.dumps(out_data, indent=2))
            return 0

        print(f"Task Trace: {task.task_id} [{task.status.value}]")
        print(f"Goal: {task.goal}")
        print("=" * 60)
        for s in steps:
            print(f"Step {s.position}: {s.title} [{s.status.value}]")
            s_cps = [cp for cp in checkpoints if cp.step_id == s.step_id]
            if s_cps:
                print("  Checkpoints:")
                for cp in s_cps:
                    consumed_str = " (consumed)" if cp.consumed_by_verifier else ""
                    print(f"    - {cp.kind}: {cp.source} [hash={cp.evidence_hash[:8]}]{consumed_str}")
            else:
                print("  Checkpoints: None")
            ver_status = s.verification_status.value if s.verification_status else "unverified"
            print(f"  Verification: {ver_status} - {s.verification_summary or 'No summary'}")
        return 0
    raise ValueError("unknown eval command")

