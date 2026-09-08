"""Deterministic capability routing pipeline for Tieru."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tieru.capabilities.catalog import build_capability_catalog
from tieru.capabilities.models import (
    CapabilityMatch,
    CapabilityRouterConfig,
    CapabilityRoutingResult,
)
from tieru.capabilities.retrieval import (
    CapabilityEmbeddingCache,
    EmbeddingBackend,
    classify_operations,
    compute_lexical_scores,
    cosine_similarity,
    has_destructive_intent,
    metadata_hash,
    normalize_text,
    tokenize,
)
from tieru.context import ContextTrust

if TYPE_CHECKING:
    from tieru.tools.registry import Tool, ToolRegistry


class CapabilityRouter:
    """Deterministic capability router selecting a bounded, explainable tool subset."""

    def __init__(
        self,
        config: CapabilityRouterConfig | None = None,
        *,
        embedding_backend: EmbeddingBackend | None = None,
        cache: CapabilityEmbeddingCache | None = None,
    ) -> None:
        self.config = config or CapabilityRouterConfig()
        self.embedding_backend = embedding_backend
        self.cache = cache or CapabilityEmbeddingCache()

    def route(
        self,
        query: str,
        registry: ToolRegistry,
        *,
        context_trust: ContextTrust = ContextTrust.USER,
        observer=None,
        replay=None,
        run_id: str = "",
    ) -> CapabilityRoutingResult:
        """Deterministically route capabilities and select model-visible tools."""
        registered_tools: dict[str, Tool] = getattr(registry, "_tools", {})
        total_tool_count = len(registered_tools)

        # 1. Mandatory/always_visible tools
        mandatory_tools: list[str] = [
            t.name for t in registered_tools.values()
            if t.always_visible or t.name in self.config.mandatory_tools
        ]

        if context_trust is ContextTrust.DATA:
            return CapabilityRoutingResult(
                selected_capabilities=(),
                selected_tools=tuple(mandatory_tools),
                truncated=False,
                candidate_tool_count=total_tool_count,
                reason="untrusted_data_rejected",
            )

        if not registered_tools:
            return CapabilityRoutingResult(
                selected_capabilities=(),
                selected_tools=(),
                truncated=False,
                candidate_tool_count=0,
                reason="empty_registry",
            )

        catalog = build_capability_catalog(registry)
        norm_query = normalize_text(query)
        query_tokens = tokenize(query)
        intended_ops = classify_operations(query)
        destructive = has_destructive_intent(query)

        # 2 & 3. Explicit references and exact alias matching
        # Context Firewall: untrusted DATA cannot use explicit naming to force tool exposure
        explicit_matches: dict[str, tuple[float, str, str | None]] = {}

        if context_trust is not ContextTrust.DATA:
            for cap in catalog.values():
                # Check explicit tool name in query tokens
                for tool_name in cap.tool_names:
                    norm_tool = normalize_text(tool_name)
                    # Word boundary check for tool name
                    if norm_tool and (
                        norm_tool in query_tokens
                        or f" {norm_tool} " in f" {norm_query} "
                        or norm_query == norm_tool
                    ):
                        explicit_matches[cap.capability_id] = (1.0, "explicit_reference", tool_name)
                        break
                if cap.capability_id in explicit_matches:
                    continue

                # Check explicit capability name
                norm_cap_name = normalize_text(cap.name)
                if norm_cap_name and (
                    norm_cap_name in norm_query
                    or any(t in query_tokens for t in tokenize(cap.name))
                ) and (
                    f" {norm_cap_name} " in f" {norm_query} " or norm_query == norm_cap_name
                ):
                    explicit_matches[cap.capability_id] = (1.0, "explicit_reference", cap.name)
                    continue

                # Check exact alias match
                for alias in cap.aliases:
                    norm_alias = normalize_text(alias)
                    if norm_alias and (
                        f" {norm_alias} " in f" {norm_query} "
                        or norm_query == norm_alias
                    ):
                        explicit_matches[cap.capability_id] = (0.95, "exact_alias", alias)
                        break

        # 4. Lexical BM25 ranking
        lex_scores = compute_lexical_scores(
            list(catalog.values()), query_tokens, self.config.bm25_k1
        )

        # 5. Optional Semantic ranking
        sem_scores: dict[str, float | None] = {}
        if self.config.semantic_enabled and self.embedding_backend is not None and query.strip():
            try:
                query_vec = self.embedding_backend.embed(query)
                for cap in catalog.values():
                    h = metadata_hash(cap)
                    cached = self.cache.get(cap.capability_id, h, self.embedding_backend.model)
                    if cached is None:
                        cached = tuple(self.embedding_backend.embed(cap.description))
                        self.cache.put(cap.capability_id, h, self.embedding_backend.model, cached)
                    sem_scores[cap.capability_id] = cosine_similarity(query_vec, cached)
            except Exception:
                # Semantic failure fallback: continue with lexical only
                sem_scores = {cap.capability_id: None for cap in catalog.values()}
        else:
            sem_scores = {cap.capability_id: None for cap in catalog.values()}

        # Combine scores
        matches: list[CapabilityMatch] = []
        for cap_id, cap in catalog.items():
            lex = lex_scores.get(cap_id, 0.0)
            sem = sem_scores.get(cap_id)
            if sem is not None:
                combined = self.config.lexical_weight * lex + self.config.semantic_weight * sem
                reason = "hybrid"
            else:
                combined = lex
                reason = "lexical"

            matched_alias = None
            if cap_id in explicit_matches:
                exp_score, exp_reason, matched_alias = explicit_matches[cap_id]
                if exp_score > combined:
                    combined = exp_score
                    reason = exp_reason

            if combined >= self.config.min_score:
                matches.append(
                    CapabilityMatch(
                        capability_id=cap_id,
                        score=round(combined, 4),
                        lexical_score=round(lex, 4),
                        semantic_score=round(sem, 4) if sem is not None else None,
                        reason=reason,
                        tool_names=cap.tool_names,
                        matched_alias=matched_alias,
                    )
                )

        # Sort matches by score descending
        matches.sort(key=lambda m: m.score, reverse=True)
        top_matches = tuple(matches[: self.config.top_k])

        # Tool Expansion & Operation Filtering
        selected_tools: list[str] = list(mandatory_tools)
        for match in top_matches:
            cap = catalog[match.capability_id]
            for t_name in cap.tool_names:
                t = registered_tools.get(t_name)
                if t is None:
                    continue

                # Filter destructive tools if no destructive intent
                is_destructive = bool(
                    t.operation in ("delete", "remove", "cancel", "wipe", "destroy")
                    or any(k in t.name.lower() for k in ("delete", "remove", "cancel", "wipe", "destroy", "forget"))
                )
                if is_destructive and not destructive and self.config.filter_destructive:
                    continue

                # Read vs write separation for calendar and similar tools
                # If user intent is strictly read and the capability has dedicated read tools,
                # do not expose write tools for an ambiguous or read-only query.
                if (
                    intended_ops == {"read"}
                    and not destructive
                    and not t.read_only
                    and any(registered_tools.get(other_t, t).read_only for other_t in cap.tool_names)
                ):
                    # Dedicated read tools exist in this capability; hide write tools
                    continue

                if t_name not in selected_tools:
                    selected_tools.append(t_name)

        # Hard visible tool count limit
        truncated = False
        if len(selected_tools) > self.config.max_visible_tools:
            truncated = True
            final_tools = tuple(selected_tools[: self.config.max_visible_tools])
        else:
            final_tools = tuple(selected_tools)

        result = CapabilityRoutingResult(
            selected_capabilities=top_matches,
            selected_tools=final_tools,
            truncated=truncated,
            candidate_tool_count=total_tool_count,
            reason="routed" if top_matches else "no_match",
        )

        # Record safe Replay / observability event
        payload = {
            "selected_capabilities": [m.capability_id for m in top_matches],
            "selected_tools": list(final_tools),
            "candidate_count": total_tool_count,
            "reasons": {m.capability_id: m.reason for m in top_matches},
            "scores": {m.capability_id: m.score for m in top_matches},
            "truncated": truncated,
            "semantic_enabled": bool(self.config.semantic_enabled and self.embedding_backend is not None),
        }
        if observer is not None:
            observer("capability_routed", payload)
        if replay is not None and hasattr(replay, "record_event"):
            replay.record_event(
                run_id,
                "capability_routed",
                payload,
            )

        return result
