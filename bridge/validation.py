from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from bridge.config import BridgeConfig, Settings

RULESET_LIBRARY_WILDCARDS = {"movie": "ALL_MOVIES", "show": "ALL_TV"}


def issue(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def validate_static_config(config: BridgeConfig, settings: Settings) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if settings.trust_forwarded_headers:
        issues.append(
            issue(
                "settings.trust_forwarded_headers",
                "TRUST_FORWARDED_HEADERS is enabled; only enable it behind a "
                "trusted reverse proxy that sanitizes X-Forwarded-* headers",
                "warning",
            )
        )
    for profile in config.profiles:
        if not profile.media_types:
            issues.append(
                issue(
                    "profile.empty_media_types",
                    f"profile {profile.id} has no media_types",
                    "error",
                )
            )
        append_external_id_issue(
            issues,
            profile.trash_profile_id,
            "profile.invalid_trash_profile_id",
            f"profile {profile.id} trash_profile_id has invalid characters",
        )
        append_external_id_issue(
            issues,
            profile.profilarr_profile_id,
            "profile.invalid_profilarr_profile_id",
            f"profile {profile.id} profilarr_profile_id has invalid characters",
        )
        for group_id in profile.trash_custom_format_group_ids:
            append_external_id_issue(
                issues,
                group_id,
                "profile.invalid_trash_group_id",
                (
                    f"profile {profile.id} has invalid TRaSH custom-format "
                    f"group id: {group_id!r}"
                ),
                required=True,
            )
        if profile.trash_profile_url and not str(profile.trash_profile_url).startswith("https://"):
            issues.append(
                issue(
                    "profile.invalid_trash_url",
                    f"profile {profile.id} trash_profile_url must be an HTTPS URL",
                    "error",
                )
            )

    mm_config_path = settings.mediamanager_config_path
    if mm_config_path:
        issues.extend(validate_mediamanager_config_file(config, mm_config_path))
    return issues


def validate_mediamanager_config_file(
    config: BridgeConfig, path: Path
) -> list[dict[str, str]]:
    if not path.exists():
        return [
            issue(
                "mediamanager_config.missing",
                f"MediaManager config path does not exist: {path}",
                "warning",
            )
        ]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [
            issue(
                "mediamanager_config.invalid_toml",
                f"MediaManager config TOML is invalid: {exc}",
                "error",
            )
        ]
    except OSError as exc:
        return [
            issue(
                "mediamanager_config.unreadable",
                f"MediaManager config path could not be read: {exc}",
                "warning",
            )
        ]

    indexers = data.get("indexers", {})
    if not isinstance(indexers, Mapping):
        return [
            issue(
                "mediamanager_config.invalid_shape",
                "MediaManager config 'indexers' section must be a table",
                "error",
            )
        ]
    scoring_rule_sets = indexers.get("scoring_rule_sets", [])
    if not is_iterable(scoring_rule_sets):
        return [
            issue(
                "mediamanager_config.invalid_shape",
                "MediaManager config 'indexers.scoring_rule_sets' must be a list",
                "error",
            )
        ]
    issues: list[dict[str, str]] = []
    append_indexer_backend_issues(indexers, issues)
    valid_title_scoring_rules = collect_valid_title_scoring_rules(indexers, issues)
    append_short_negative_keyword_issues(valid_title_scoring_rules, issues)
    parsed_rulesets: list[tuple[str, Mapping[str, Any]]] = []
    for index, ruleset in enumerate(scoring_rule_sets):
        if not isinstance(ruleset, Mapping):
            issues.append(
                issue(
                    "ruleset.invalid_entry",
                    f"MediaManager scoring ruleset entry {index} must be a table",
                    "error",
                )
            )
            continue
        if not ruleset.get("name"):
            issues.append(
                issue(
                    "ruleset.invalid_entry",
                    f"MediaManager scoring ruleset entry {index} is missing a name",
                    "error",
                )
            )
            continue
        ruleset_name = str(ruleset["name"])
        parsed_rulesets.append((ruleset_name, ruleset))
    rule_names = _configured_rule_names(indexers, issues, valid_title_scoring_rules)

    duplicate_rulesets = append_duplicate_name_issues(
        issues,
        (ruleset_name for ruleset_name, _ruleset in parsed_rulesets),
        "ruleset.duplicate_name",
        "MediaManager scoring ruleset name is duplicated",
    )
    rulesets = {
        ruleset_name: ruleset
        for ruleset_name, ruleset in parsed_rulesets
        if ruleset_name not in duplicate_rulesets
    }

    for profile in config.profiles:
        ruleset = rulesets.get(profile.mediamanager_ruleset)
        if ruleset is None:
            issues.append(
                issue(
                    "ruleset.missing",
                    (
                        f"profile {profile.id} references missing MediaManager ruleset "
                        f"{profile.mediamanager_ruleset!r}"
                    ),
                    "error",
                )
            )
            continue

        configured_rule_names = ruleset.get("rule_names", [])
        if not is_iterable(configured_rule_names):
            issues.append(
                issue(
                    "ruleset.invalid_rule_names",
                    f"ruleset {profile.mediamanager_ruleset!r} rule_names must be a list",
                    "error",
                )
            )
            continue
        missing_rules = [str(name) for name in configured_rule_names if str(name) not in rule_names]
        if missing_rules:
            issues.append(
                issue(
                    "ruleset.missing_rules",
                    (
                        f"ruleset {profile.mediamanager_ruleset!r} references missing "
                        f"rules: {', '.join(missing_rules)}"
                    ),
                    "error",
                )
            )

        configured_libraries = ruleset.get("libraries", [])
        if not is_iterable(configured_libraries):
            issues.append(
                issue(
                    "ruleset.invalid_libraries",
                    f"ruleset {profile.mediamanager_ruleset!r} libraries must be a list",
                    "error",
                )
            )
            continue
        libraries = {str(name) for name in configured_libraries}
        missing_media_types: list[str] = []
        for media_type in profile.media_types:
            wildcard = RULESET_LIBRARY_WILDCARDS[media_type]
            if profile.mediamanager_library not in libraries and wildcard not in libraries:
                missing_media_types.append(media_type)
        if missing_media_types:
            expected_wildcards = [
                RULESET_LIBRARY_WILDCARDS[media_type]
                for media_type in missing_media_types
            ]
            issues.append(
                issue(
                    "ruleset.library_drift",
                    (
                        f"profile {profile.id} maps shared {', '.join(missing_media_types)} "
                        f"library {profile.mediamanager_library!r}, but ruleset "
                        f"{profile.mediamanager_ruleset!r} does not include that "
                        f"library or {', '.join(expected_wildcards)}"
                    ),
                    "warning",
                )
            )

    return issues


def append_indexer_backend_issues(
    indexers: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> None:
    prowlarr_configured = "prowlarr" in indexers
    prowlarr = indexer_backend_config(indexers, issues, "prowlarr")
    if not prowlarr_configured or (
        prowlarr is not None and not bool(prowlarr.get("enabled"))
    ):
        issues.append(
            issue(
                "indexer.prowlarr_disabled",
                (
                    "MediaManager config has Prowlarr missing or disabled; Screenarr docs "
                    "assume Prowlarr as the primary indexer path."
                ),
                "warning",
            )
        )
    jackett = indexer_backend_config(indexers, issues, "jackett")
    if jackett is not None and bool(jackett.get("enabled")):
        issues.append(
            issue(
                "indexer.jackett_enabled",
                (
                    "MediaManager config has Jackett enabled; use Prowlarr as "
                    "the primary path when possible."
                ),
                "warning",
            )
        )


def indexer_backend_config(
    indexers: Mapping[str, Any],
    issues: list[dict[str, str]],
    name: str,
) -> Mapping[str, Any] | None:
    backend = indexers.get(name)
    if backend is None:
        return None
    if isinstance(backend, Mapping):
        return backend
    issues.append(
        issue(
            "indexer.invalid_shape",
            f"MediaManager config 'indexers.{name}' must be a table",
            "error",
        )
    )
    return None


def collect_valid_title_scoring_rules(
    indexers: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> list[tuple[Mapping[str, Any], float, list[str]]]:
    configured_rules = indexers.get("title_scoring_rules")
    rules = [] if configured_rules is None else configured_rules
    if not is_iterable(rules):
        issues.append(
            issue(
                "indexer.invalid_title_scoring_rules",
                "MediaManager config 'indexers.title_scoring_rules' must be a list",
                "error",
            )
        )
        return []
    parsed_rules: list[tuple[Mapping[str, Any], float, list[str]]] = []
    rule_name_counts: dict[str, int] = {}
    for rule in rules:
        valid_rule = validate_title_scoring_rule(rule, issues)
        if valid_rule is not None:
            rule_name = str(valid_rule[0]["name"])
            rule_name_counts[rule_name] = rule_name_counts.get(rule_name, 0) + 1
            parsed_rules.append(valid_rule)
    append_duplicate_name_issues(
        issues,
        rule_name_counts.keys(),
        "indexer.duplicate_title_scoring_rule_name",
        "MediaManager title scoring rule name is duplicated",
        counts=rule_name_counts,
    )
    return parsed_rules


def validate_title_scoring_rule(
    rule: object,
    issues: list[dict[str, str]],
) -> tuple[Mapping[str, Any], float, list[str]] | None:
    if not isinstance(rule, Mapping):
        issues.append(
            issue(
                "indexer.invalid_title_scoring_rule",
                "title scoring rule entry must be a table",
                "error",
            )
        )
        return None
    rule_name = rule.get("name")
    if not isinstance(rule_name, str) or not rule_name.strip():
        issues.append(
            issue(
                "indexer.invalid_title_scoring_rule_name",
                "title scoring rule name must be a non-empty string",
                "error",
            )
        )
        return None
    score = numeric_score(rule.get("score_modifier"))
    if score is None:
        issues.append(
            issue(
                "indexer.invalid_score_modifier",
                f"title scoring rule {rule_name!r} score_modifier must be numeric",
                "error",
            )
        )
        return None
    keyword_values = validate_title_scoring_keywords(rule, rule_name, issues)
    if keyword_values is None:
        return None
    return rule, score, keyword_values


def validate_title_scoring_keywords(
    rule: Mapping[str, Any],
    rule_name: str,
    issues: list[dict[str, str]],
) -> list[str] | None:
    keywords = rule.get("keywords", [])
    if not is_iterable(keywords):
        issues.append(
            issue(
                "indexer.invalid_keywords",
                f"title scoring rule {rule_name!r} keywords must be a list",
                "error",
            )
        )
        return None
    keyword_values: list[str] = []
    for keyword in keywords:
        if isinstance(keyword, str) and keyword.strip():
            keyword_values.append(keyword)
            continue
        message = (
            "must be non-empty strings"
            if isinstance(keyword, str)
            else "must be strings"
        )
        issues.append(
            issue(
                "indexer.invalid_keyword",
                f"title scoring rule {rule_name!r} keywords {message}",
                "error",
            )
        )
        return None
    return keyword_values


def append_short_negative_keyword_issues(
    valid_title_scoring_rules: list[tuple[Mapping[str, Any], float, list[str]]],
    issues: list[dict[str, str]],
) -> None:
    for rule, score, keywords in valid_title_scoring_rules:
        rule_name = str(rule["name"])
        if score >= 0:
            continue
        short_keywords = sorted(short_nonempty_keywords(keywords))
        if short_keywords:
            issues.append(
                issue(
                    "indexer.short_negative_keyword",
                    (
                        f"title scoring rule {rule_name!r} "
                        f"negatively scores very short keywords: {', '.join(short_keywords)}"
                    ),
                    "warning",
                )
            )


def append_external_id_issue(
    issues: list[dict[str, str]],
    value: str | None,
    code: str,
    message: str,
    *,
    required: bool = False,
) -> None:
    if value is None or (value == "" and not required):
        return
    if not valid_external_id(value):
        issues.append(issue(code, message, "error"))


def _configured_rule_names(
    indexers: Mapping[str, Any],
    issues: list[dict[str, str]],
    valid_title_scoring_rules: list[tuple[Mapping[str, Any], float, list[str]]],
) -> set[str]:
    names: set[str] = set()
    title_rule_names: set[str] = set()
    for rule, _score, _keywords in valid_title_scoring_rules:
        title_rule_names.add(str(rule["name"]))
    names.update(title_rule_names)
    rules = indexers.get("indexer_flag_scoring_rules", [])
    if not is_iterable(rules):
        issues.append(
            issue(
                "indexer.invalid_flag_scoring_rules",
                "MediaManager config 'indexers.indexer_flag_scoring_rules' must be a list",
                "error",
            )
        )
        return names
    flag_rule_names: list[str] = []
    flag_rule_name_counts: dict[str, int] = {}
    for rule in rules:
        if not isinstance(rule, Mapping):
            issues.append(
                issue(
                    "indexer.invalid_flag_scoring_rule",
                    "indexer flag scoring rule entry must be a table",
                    "error",
                )
            )
            continue
        rule_name = rule.get("name")
        if not isinstance(rule_name, str) or not rule_name.strip():
            issues.append(
                issue(
                    "indexer.invalid_flag_scoring_rule_name",
                    "indexer flag scoring rule name must be a non-empty string",
                    "error",
                )
            )
            continue
        flag_rule_names.append(rule_name)
        flag_rule_name_counts[rule_name] = flag_rule_name_counts.get(rule_name, 0) + 1
    duplicate_flag_rule_names = append_duplicate_name_issues(
        issues,
        flag_rule_name_counts.keys(),
        "ruleset.duplicate_indexer_flag_scoring_rule_name",
        "MediaManager indexer flag scoring rule name is duplicated",
        counts=flag_rule_name_counts,
    )
    unique_flag_rule_names = {
        rule_name
        for rule_name in flag_rule_names
        if rule_name not in duplicate_flag_rule_names
    }
    cross_source_duplicates = title_rule_names & unique_flag_rule_names
    issues.extend(
        issue(
            "ruleset.duplicate_scoring_rule_name",
            f"MediaManager scoring rule name is duplicated across rule types: {name!r}",
            "error",
        )
        for name in sorted(cross_source_duplicates)
    )
    names.update(unique_flag_rule_names - cross_source_duplicates)
    return names - cross_source_duplicates


def append_duplicate_name_issues(
    issues: list[dict[str, str]],
    names: Iterable[str],
    code: str,
    message_prefix: str,
    *,
    counts: Mapping[str, int] | None = None,
) -> set[str]:
    duplicate_names = find_duplicate_names(counts or count_names(names))
    issues.extend(
        issue(code, f"{message_prefix}: {name!r}", "error")
        for name in sorted(duplicate_names)
    )
    return duplicate_names


def find_duplicate_names(counts: Mapping[str, int]) -> set[str]:
    return {name for name, count in counts.items() if count > 1}


def count_names(names: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts


def is_iterable(value: object) -> bool:
    return (
        isinstance(value, Iterable)
        and not isinstance(value, Mapping)
        and not isinstance(value, str | bytes)
    )


def valid_external_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.:/-]+", value))


def short_nonempty_keywords(keywords: list[str]) -> set[str]:
    stripped_keywords = (keyword.strip() for keyword in keywords)
    return {keyword for keyword in stripped_keywords if 0 < len(keyword) <= 2}


def numeric_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        score = float(value)
    except OverflowError:
        return None
    if not isfinite(score):
        return None
    return score
