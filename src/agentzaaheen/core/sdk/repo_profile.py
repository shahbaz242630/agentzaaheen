"""What a project is built with: its languages and the outside providers it uses (F17 slice 5b-1).

A code-hosting connector reads one repository and returns a
:class:`RepoProfile`: the languages the host counted (bytes per language, as
GitHub's bar shows them) and the providers **code** found in it: GitHub,
Cloudflare, RevenueCat, the App Store, Google Play, Stripe, Supabase and so on.
No model is asked: each provider is found by a fixed rule (:data:`PROVIDERS`)
and comes with the evidence that matched, so the Chairman can see why.

Evidence comes from three places only:

- **file paths** from the repository's tree (``wrangler.jsonc``, ``ios/``);
- **package names** read from the project's manifests (:func:`dependencies`):
  ``package.json``, ``pyproject.toml``, ``requirements.txt``, ``app.json``,
  workflow files and a few more;
- **web addresses** a provider owns (:data:`HOSTS`: ``supabase.co``,
  ``api.paddle.com``) found in a few code and config files
  (:func:`pick_code`), for code that calls a provider without its package.

File text is untrusted and is only matched against the rules here, never
kept, shown or passed to a model. Evidence names a path and, at most, a
package name that matched a rule and is plain, or one of our own addresses.

Secret files are never read (the same list as :mod:`repo_files`).
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase, translate
from functools import cache
from pathlib import PurePosixPath

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agentzaaheen.core.sdk.code import REPO_PATTERN
from agentzaaheen.core.sdk.repo_files import is_plain_path, is_secret_path

_BRANCH = r"^[A-Za-z0-9._/-]{1,255}$"  # as in RepoHealth
_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_LANGUAGE = r"^[A-Za-z0-9 +#.'()_-]{1,60}$"  # GitHub's language names ("C++", "Jupyter Notebook")
_PACKAGE = re.compile(r"[a-z0-9@][a-z0-9@/._:+-]{0,99}")

MAX_LANGUAGES = 60
MAX_PROVIDERS = 60
MAX_EVIDENCE = 3  # evidence kept per provider
MAX_MANIFESTS = 16  # manifest files read
MAX_CODE_FILES = 24  # code and config files searched for providers' web addresses
MAX_CODE_BYTES = 60_000  # larger code files are not read
MAX_MANIFEST_BYTES = 100_000  # larger manifests are not read
SHARE_WHOLE = 10_000  # basis points: language shares add up to exactly this


class ProviderCategory(StrEnum):
    """How the workspace page groups providers."""

    CODE = "code"  # where the code lives and how it is built
    HOSTING = "hosting"
    STORES = "stores"  # app stores
    PAYMENTS = "payments"
    DATA = "data"  # databases and storage
    MONITORING = "monitoring"  # errors and analytics
    MESSAGES = "messages"  # email, texts, push
    AI = "ai"
    SIGN_IN = "sign_in"


@dataclass(frozen=True)
class ProviderRule:
    """How code recognises one provider. Patterns are matched in lower case.

    ``folders``: a folder anywhere on a file's path (``ios``, ``*.xcodeproj``);
    ``files``: a file's name (``wrangler.toml``, ``podfile``); ``packages``: a
    name :func:`dependencies` found in a manifest (``stripe``, ``@stripe/*``);
    ``words``: a brand name inside a **code** file's path (``paddle`` matches
    ``src/paddle_webhook.rs``), for code that calls a provider without its
    package. Only distinctive brand names are words, never ordinary ones.
    """

    id: str
    name: str
    category: ProviderCategory
    folders: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    words: tuple[str, ...] = ()


_C = ProviderCategory
PROVIDERS: tuple[ProviderRule, ...] = (
    # ---- code and builds
    ProviderRule("github", "GitHub", _C.CODE, folders=(".github",)),
    ProviderRule(
        "expo", "Expo (EAS)", _C.CODE, files=("eas.json",), packages=("expo", "eas-cli", "action:expo/*")
    ),
    # ---- hosting
    ProviderRule(
        "cloudflare",
        "Cloudflare",
        _C.HOSTING,
        files=("wrangler.toml", "wrangler.json", "wrangler.jsonc"),
        packages=("wrangler", "@cloudflare/*", "action:cloudflare/*"),
    ),
    ProviderRule("vercel", "Vercel", _C.HOSTING, files=("vercel.json",), packages=("vercel", "@vercel/*")),
    ProviderRule(
        "netlify", "Netlify", _C.HOSTING, files=("netlify.toml",), packages=("netlify-cli", "@netlify/*")
    ),
    ProviderRule(
        "aws",
        "Amazon Web Services",
        _C.HOSTING,
        packages=("@aws-sdk/*", "aws-sdk", "aws-cdk-lib", "boto3", "action:aws-actions/*"),
    ),
    ProviderRule(
        "google-cloud",
        "Google Cloud",
        _C.HOSTING,
        packages=("@google-cloud/*", "googleapis", "google-cloud-*", "action:google-github-actions/*"),
    ),
    ProviderRule(
        "azure",
        "Microsoft Azure",
        _C.HOSTING,
        folders=(".azure",),
        files=("*.bicep", "azure-pipelines.yml", "azure.yaml"),
        packages=("@azure/*", "azure-*", "action:azure/*"),
    ),
    ProviderRule("fly", "Fly.io", _C.HOSTING, files=("fly.toml",), packages=("action:superfly/*",)),
    ProviderRule("render", "Render", _C.HOSTING, files=("render.yaml",)),
    ProviderRule("railway", "Railway", _C.HOSTING, files=("railway.json", "railway.toml")),
    # ---- app stores
    ProviderRule(
        "apple",
        "Apple App Store",
        _C.STORES,
        folders=("ios", "*.xcodeproj", "*.xcworkspace"),
        files=("podfile",),
        packages=("expo.ios", "expo-apple-authentication", "@invertase/react-native-apple-authentication"),
    ),
    ProviderRule(
        "google-play",
        "Google Play",
        _C.STORES,
        folders=("android",),
        files=("androidmanifest.xml",),
        packages=("expo.android",),
    ),
    # ---- payments
    ProviderRule(
        "revenuecat",
        "RevenueCat",
        _C.PAYMENTS,
        packages=(
            "react-native-purchases",
            "react-native-purchases-ui",
            "@revenuecat/*",
            "purchases-js",
            "revenuecat",
            "purchases_flutter",
        ),
        words=("revenuecat",),
    ),
    ProviderRule("stripe", "Stripe", _C.PAYMENTS, packages=("stripe", "@stripe/*"), words=("stripe",)),
    ProviderRule(
        "paddle",
        "Paddle",
        _C.PAYMENTS,
        packages=("@paddle/*", "paddle-billing", "paddle-python-sdk"),
        words=("paddle",),
    ),
    ProviderRule("lemon-squeezy", "Lemon Squeezy", _C.PAYMENTS, packages=("@lemonsqueezy/*",)),
    # ---- data
    ProviderRule(
        "supabase",
        "Supabase",
        _C.DATA,
        folders=("supabase",),
        packages=("@supabase/*", "supabase", "supabase_flutter"),
        words=("supabase",),
    ),
    ProviderRule(
        "firebase",
        "Google Firebase",
        _C.DATA,
        files=("firebase.json", "google-services.json", "googleservice-info.plist"),
        packages=("firebase", "firebase-admin", "firebase-tools", "@react-native-firebase/*", "@firebase/*"),
        words=("firebase",),
    ),
    ProviderRule("mongodb", "MongoDB", _C.DATA, packages=("mongodb", "mongoose", "pymongo", "motor")),
    ProviderRule("neon", "Neon", _C.DATA, packages=("@neondatabase/*",)),
    ProviderRule("upstash", "Upstash", _C.DATA, packages=("@upstash/*", "upstash-redis")),
    # ---- errors and analytics
    ProviderRule(
        "sentry",
        "Sentry",
        _C.MONITORING,
        files=("sentry.properties",),
        packages=("@sentry/*", "sentry-sdk", "sentry_sdk", "sentry_flutter", "sentry"),
        words=("sentry",),
    ),
    ProviderRule(
        "posthog",
        "PostHog",
        _C.MONITORING,
        packages=("posthog", "posthog-js", "posthog-node", "posthog-react-native", "posthog-python"),
        words=("posthog",),
    ),
    ProviderRule("mixpanel", "Mixpanel", _C.MONITORING, packages=("mixpanel", "mixpanel-*")),
    ProviderRule("amplitude", "Amplitude", _C.MONITORING, packages=("@amplitude/*", "amplitude-*")),
    ProviderRule("segment", "Segment", _C.MONITORING, packages=("@segment/*", "analytics-node")),
    ProviderRule("grafana", "Grafana Cloud", _C.MONITORING, packages=("@grafana/*",)),
    # ---- messages
    ProviderRule("resend", "Resend", _C.MESSAGES, packages=("resend",)),
    ProviderRule("sendgrid", "SendGrid", _C.MESSAGES, packages=("@sendgrid/*", "sendgrid")),
    ProviderRule("postmark", "Postmark", _C.MESSAGES, packages=("postmark",)),
    ProviderRule("mailgun", "Mailgun", _C.MESSAGES, packages=("mailgun.js", "mailgun")),
    ProviderRule("twilio", "Twilio", _C.MESSAGES, packages=("twilio",), words=("twilio",)),
    ProviderRule(
        "onesignal", "OneSignal", _C.MESSAGES, packages=("react-native-onesignal", "onesignal-*", "onesignal")
    ),
    # ---- AI
    ProviderRule(
        "openai", "OpenAI", _C.AI, packages=("openai", "async-openai", "@ai-sdk/openai"), words=("openai",)
    ),
    ProviderRule(
        "anthropic",
        "Anthropic (Claude)",
        _C.AI,
        packages=("@anthropic-ai/*", "anthropic", "@ai-sdk/anthropic"),
    ),
    ProviderRule(
        "google-gemini",
        "Google Gemini",
        _C.AI,
        packages=(
            "@google/genai",
            "@google/generative-ai",
            "google-genai",
            "google-generativeai",
            "@ai-sdk/google",
        ),
    ),
    # ---- sign-in
    ProviderRule("clerk", "Clerk", _C.SIGN_IN, packages=("@clerk/*",)),
    ProviderRule("auth0", "Auth0", _C.SIGN_IN, packages=("auth0", "@auth0/*", "react-native-auth0")),
)

# Manifests whose package names are read, matched against the file's name, up to two folders down.
_MANIFESTS = (
    "package.json", "pyproject.toml", "app.json", "podfile", "gemfile", "pubspec.yaml", "go.mod",
    "composer.json", "cargo.toml",
)  # fmt: skip
_REQUIREMENTS = "requirements*.txt"
# Folders whose files describe other projects (samples, fixtures, docs, copies of libraries): never evidence.
_SAMPLE_FOLDERS = frozenset(
    {"fixtures", "__fixtures__", "testdata", "examples", "example", "samples", "sample", "docs", "doc",
     "vendor", "third_party", "third-party", "__mocks__", "mocks"}
)  # fmt: skip
_TEST_FOLDERS = frozenset({"test", "tests", "__tests__", "spec", "e2e"})
_MANIFEST_DEPTH = 2
# Code files, where a brand word in the path means the code talks to that provider.
_CODE_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".py",
        ".swift",
        ".kt",
        ".dart",
        ".go",
        ".rb",
        ".rs",
        ".java",
        ".php",
        ".cs",
    }
)
_MAX_WORKFLOWS = 6
_WORKFLOWS = ".github/workflows/"

# Web addresses that belong to one provider, so finding one in code means the code talks to it.
# A host matches itself or any name ending in it (``abcd.supabase.co`` matches ``supabase.co``).
HOSTS: Mapping[str, tuple[str, ...]] = {
    "cloudflare": ("api.cloudflare.com", "workers.dev", "pages.dev", "r2.cloudflarestorage.com"),
    "vercel": ("vercel.app", "api.vercel.com"),
    "netlify": ("netlify.app", "api.netlify.com"),
    "aws": ("amazonaws.com",),
    "azure": ("azurewebsites.net", "azurecontainerapps.io", "blob.core.windows.net", "management.azure.com"),
    "fly": ("fly.dev",),
    "render": ("onrender.com",),
    "railway": ("railway.app",),
    "apple": ("buy.itunes.apple.com", "api.storekit.itunes.apple.com", "api.appstoreconnect.apple.com"),
    "google-play": ("androidpublisher.googleapis.com",),
    "expo": ("exp.host", "api.expo.dev"),
    "revenuecat": ("api.revenuecat.com",),
    "stripe": ("api.stripe.com", "js.stripe.com", "checkout.stripe.com"),
    "paddle": ("api.paddle.com", "sandbox-api.paddle.com", "cdn.paddle.com"),
    "lemon-squeezy": ("api.lemonsqueezy.com",),
    "supabase": ("supabase.co", "supabase.in"),
    "firebase": ("firebaseio.com", "firebaseapp.com", "firestore.googleapis.com"),
    "mongodb": ("mongodb.net",),
    "neon": ("neon.tech",),
    "upstash": ("upstash.io",),
    "sentry": ("sentry.io",),
    "posthog": ("posthog.com",),
    "mixpanel": ("api.mixpanel.com", "api-js.mixpanel.com"),
    "amplitude": ("api.amplitude.com", "api2.amplitude.com"),  # gitleaks:allow (public addresses)
    "segment": ("api.segment.io", "cdn.segment.com"),
    "grafana": ("grafana.net",),
    "resend": ("api.resend.com",),
    "sendgrid": ("api.sendgrid.com",),
    "postmark": ("api.postmarkapp.com",),
    "mailgun": ("api.mailgun.net", "api.eu.mailgun.net"),
    "twilio": ("api.twilio.com",),
    "onesignal": ("onesignal.com",),
    "openai": ("api.openai.com",),
    "anthropic": ("api.anthropic.com",),
    "google-gemini": ("generativelanguage.googleapis.com",),
    "clerk": ("clerk.accounts.dev", "api.clerk.com"),
    "auth0": ("auth0.com",),
}
_HOST_OF = {host: provider for provider, hosts in HOSTS.items() for host in hosts}
_HOST = re.compile(
    r"(?<![a-z0-9.-])(?:[a-z0-9-]+\.)*("
    + "|".join(re.escape(h) for h in sorted(_HOST_OF, key=len, reverse=True))
    + r")(?![a-z0-9-]|\.[a-z0-9])"
)
# Config files that often name the addresses an app calls, read first.
_CONFIGS = (
    "wrangler.toml", "wrangler.json", "wrangler.jsonc", "eas.json", "vercel.json", "netlify.toml",
    "fly.toml", "app.config.ts", "app.config.js", "next.config.js", "next.config.mjs", "next.config.ts",
)  # fmt: skip
# Code file names or folders that usually hold the calls to outside services.
_CALLERS = re.compile(
    r"client|api|config|const|env|setting|service|lib|http|fetch|db|data|storage|auth|pay|bill|mail"
    r"|notif|analytic|sync|upload|webhook|worker|server|index|main"
)
_CODE_DEPTH = 4


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Language(_Shape):
    """One language and how many bytes of it the host counted."""

    name: str = Field(pattern=_LANGUAGE)
    bytes: int = Field(ge=0)


class Provider(_Shape):
    """One outside provider code found, with its reasons (``path`` or ``path: package``)."""

    id: str = Field(pattern=_ID, max_length=40)
    name: str = Field(max_length=60)
    category: ProviderCategory
    evidence: tuple[str, ...] = Field(min_length=1, max_length=MAX_EVIDENCE)


class RepoProfile(_Shape):
    """A repository's languages (largest first) and providers (in :data:`PROVIDERS` order).

    ``changed_at`` is when the host last saw a push (``None`` if it did not say);
    ``read_at`` is when this profile was read.
    """

    repo: str = Field(pattern=REPO_PATTERN)
    branch: str = Field(pattern=_BRANCH)
    languages: tuple[Language, ...] = Field(max_length=MAX_LANGUAGES)
    providers: tuple[Provider, ...] = Field(max_length=MAX_PROVIDERS)
    files_read: int = Field(ge=0, le=MAX_MANIFESTS + MAX_CODE_FILES)
    changed_at: AwareDatetime | None
    read_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> RepoProfile:
        if len({lang.name for lang in self.languages}) != len(self.languages):
            raise ValueError("a language is listed twice")
        if list(self.languages) != sorted(self.languages, key=lambda lang: -lang.bytes):
            raise ValueError("languages must be listed largest first")
        if len({p.id for p in self.providers}) != len(self.providers):
            raise ValueError("a provider is listed twice")
        return self

    def shares(self) -> tuple[int, ...]:
        """Each language's share in basis points, adding up to exactly :data:`SHARE_WHOLE` (or all 0)."""
        return shares([lang.bytes for lang in self.languages])


def shares(sizes: Sequence[int]) -> tuple[int, ...]:
    """Basis points per size, adding up to exactly :data:`SHARE_WHOLE` (largest remainder); all 0 if empty."""
    total = sum(sizes)
    if total == 0:
        return tuple(0 for _ in sizes)
    exact = [size * SHARE_WHOLE / total for size in sizes]
    floors = [int(x) for x in exact]
    left = SHARE_WHOLE - sum(floors)
    order = sorted(range(len(sizes)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:left]:
        floors[i] += 1
    return tuple(floors)


def pick_manifests(paths: Sequence[str]) -> list[str]:
    """The manifests whose package names are read, shallowest first, at most :data:`MAX_MANIFESTS`."""
    usable = sorted(_usable(paths), key=lambda p: (p.count("/"), p.lower()))
    found = [p for p in usable if p.count("/") <= _MANIFEST_DEPTH and _is_manifest(p)]
    workflows = [p for p in usable if p.startswith(_WORKFLOWS) and p.endswith((".yml", ".yaml"))]
    workflows = workflows[:_MAX_WORKFLOWS]
    return found[: MAX_MANIFESTS - len(workflows)] + workflows


def pick_code(paths: Sequence[str]) -> list[str]:
    """The code and config files searched for providers' web addresses, at most :data:`MAX_CODE_FILES`.

    Config files first, then code files whose names or folders suggest calls to
    outside services (``api``, ``client``, ``config``...), shallowest first; tests never.
    """
    usable = sorted(
        (p for p in _usable(paths) if not _is_test(p) and p.count("/") <= _CODE_DEPTH),
        key=lambda p: (p.count("/"), p.lower()),
    )
    configs = [p for p in usable if PurePosixPath(p).name.lower() in _CONFIGS]
    callers = [
        p
        for p in usable
        if PurePosixPath(p).suffix in _CODE_SUFFIXES and _CALLERS.search(p.lower()) and p not in configs
    ]
    return (configs + callers)[:MAX_CODE_FILES]


def hosts_in(text: str) -> list[str]:
    """The providers' web addresses (from :data:`HOSTS`) named in ``text``, each once, in order found."""
    found: list[str] = []
    for match in _HOST.finditer(text.lower()):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


def _usable(paths: Iterable[str]) -> list[str]:
    """Paths that may count as evidence: plain, not secret, not in a sample or vendored folder."""
    return [
        p
        for p in paths
        if is_plain_path(p)
        and not is_secret_path(p)
        and not any(part.lower() in _SAMPLE_FOLDERS for part in p.split("/")[:-1])
    ]


def _is_test(path: str) -> bool:
    lower = path.lower()
    name = PurePosixPath(lower).name
    return any(part in _TEST_FOLDERS for part in lower.split("/")[:-1]) or any(
        mark in name for mark in (".test.", ".spec.", "_test.", "test_")
    )


def _is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in _MANIFESTS or fnmatchcase(name, _REQUIREMENTS)


def dependencies(path: str, text: str) -> frozenset[str]:
    """The package names one manifest names, in lower case. Unreadable text gives none (never a guess).

    Workflow files give the actions they use (``action:cloudflare/wrangler-action``);
    ``app.json`` gives its plugins, plus ``expo.ios`` / ``expo.android`` when it builds for them.
    """
    name = PurePosixPath(path).name.lower()
    if path.startswith(_WORKFLOWS):
        found: Iterable[str] = (f"action:{m}" for m in _USES.findall(text))
    elif name in {"package.json", "composer.json"}:
        found = _package_json(text)
    elif name == "app.json":
        found = _app_json(text)
    elif name == "pyproject.toml":
        found = _pyproject(text)
    elif name == "cargo.toml":
        found = _cargo(text)
    elif fnmatchcase(name, _REQUIREMENTS):
        found = (m.group(1) for line in text.splitlines() if (m := _REQUIREMENT.match(line)))
    else:
        pattern = _LINE_RULES.get(name)
        found = pattern.findall(text) if pattern else ()
    return frozenset(n.lower() for n in found if _PACKAGE.fullmatch(n.lower()))


_USES = re.compile(r"^\s*-?\s*uses:\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.MULTILINE)
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NAME = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._-]*")
_LINE_RULES: Mapping[str, re.Pattern[str]] = {
    "podfile": re.compile(r"^\s*pod\s+['\"]([A-Za-z0-9_.+/-]+)['\"]", re.MULTILINE),
    "gemfile": re.compile(r"^\s*gem\s+['\"]([A-Za-z0-9_.-]+)['\"]", re.MULTILINE),
    "pubspec.yaml": re.compile(r"^ {2}([a-z0-9_]+):", re.MULTILINE),
    "go.mod": re.compile(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/[A-Za-z0-9._/-]+)\s+v", re.MULTILINE),
}
_PACKAGE_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require")


def _package_json(text: str) -> list[str]:
    data = _json(text)
    return [name for key in _PACKAGE_KEYS if isinstance(deps := data.get(key), dict) for name in deps]


def _app_json(text: str) -> list[str]:
    data = _json(text)
    expo = data.get("expo", data)
    if not isinstance(expo, dict):
        return []
    found = [f"expo.{side}" for side in ("ios", "android") if isinstance(expo.get(side), dict)]
    for plugin in expo.get("plugins", []) if isinstance(expo.get("plugins"), list) else []:
        named = plugin[0] if isinstance(plugin, list) and plugin else plugin
        if isinstance(named, str):
            found.append(named)
    return found


def _pyproject(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    project = data.get("project", {})
    specs: list[object] = list(project.get("dependencies", [])) if isinstance(project, dict) else []
    groups = [project.get("optional-dependencies", {}) if isinstance(project, dict) else {}]
    groups.append(data.get("dependency-groups", {}))
    for group in groups:
        if isinstance(group, dict):
            specs += [spec for values in group.values() if isinstance(values, list) for spec in values]
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    named = list(poetry.get("dependencies", {})) if isinstance(poetry, dict) else []
    named += [m.group(0) for spec in specs if isinstance(spec, str) and (m := _NAME.match(spec))]
    return named


def _cargo(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    tables = [data.get(key) for key in ("dependencies", "dev-dependencies", "build-dependencies")]
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        tables.append(workspace.get("dependencies"))
    return [name for table in tables if isinstance(table, dict) for name in table]


def _json(text: str) -> dict[str, object]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def detect(paths: Sequence[str], texts: Mapping[str, str]) -> tuple[Provider, ...]:
    """The providers the rules find in these paths and file texts, in :data:`PROVIDERS` order.

    ``paths`` are the repository's files (secret files are skipped here too);
    ``texts`` maps a read file's path to its text: manifests give package names,
    every file gives web addresses. Each provider keeps its first
    :data:`MAX_EVIDENCE` reasons: packages first, then addresses, then paths.
    """
    usable = sorted(_usable(paths), key=_is_test)  # stable: real code first, tests after
    packages = [
        (path, name)
        for path, text in sorted(texts.items())
        if _usable([path])
        for name in sorted(dependencies(path, text))
    ]
    addresses = [
        (path, host)
        for path, text in sorted(texts.items(), key=lambda item: (_is_test(item[0]), item[0]))
        if _usable([path])
        for host in hosts_in(text)
    ]
    found: list[Provider] = []
    for rule in PROVIDERS:
        evidence = [f"{path}: {name}" for path, name in packages if _any(name, rule.packages)]
        evidence += [f"{path}: {host}" for path, host in addresses if _HOST_OF[host] == rule.id]
        evidence += _path_evidence(rule, usable)
        if evidence:
            found.append(
                Provider(
                    id=rule.id,
                    name=rule.name,
                    category=rule.category,
                    evidence=tuple(evidence[:MAX_EVIDENCE]),
                )
            )
    return tuple(found)


def _path_evidence(rule: ProviderRule, paths: Sequence[str]) -> list[str]:
    """Up to :data:`MAX_EVIDENCE` reasons from paths: the matching folder (once) or file."""
    folders, files = _matcher(rule.folders), _matcher(rule.files)
    if folders is None and files is None and not rule.words:
        return []
    found: list[str] = []
    for path in paths:
        if len(found) >= MAX_EVIDENCE:
            break
        parts = path.lower().split("/")
        at = next((i for i, part in enumerate(parts[:-1]) if folders and folders.fullmatch(part)), None)
        if at is not None:
            reason: str | None = "/".join(path.split("/")[: at + 1]) + "/"
        elif files and files.fullmatch(parts[-1]):
            reason = path
        else:
            reason = path if rule.words and _names_brand(parts, rule.words) else None
        if reason is not None and reason not in found:
            found.append(reason)
    return found


def _names_brand(parts: Sequence[str], words: Sequence[str]) -> bool:
    """True if a code file's name or folders hold one of the brand words."""
    if PurePosixPath(parts[-1]).suffix not in _CODE_SUFFIXES:
        return False
    return any(word in part for part in parts for word in words)


@cache
def _matcher(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regular expression for a rule's name patterns (thousands of paths are checked per rule)."""
    return re.compile("|".join(f"(?:{translate(p)})" for p in patterns)) if patterns else None


def _any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)
