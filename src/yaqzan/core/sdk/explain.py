"""The error catalogue: every reason code in plain English (what broke, why, what to do).

Codes stay short and machine-safe (``net.timeout``); this turns one into words
a non-engineer can act on. Logs, the dashboard and alerts all render the same
text from here, so a failure reads the same wherever it is seen.

``{vendor}`` in the text becomes the source's name ("Paddle"), or "the
service" when there is none. An unknown code still gets an answer that says
so, never an empty line.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Explanation:
    """One code in words. ``what`` broke, the likely ``why``, and ``fix``: what to do now."""

    code: str
    what: str
    why: str
    fix: str

    def line(self) -> str:
        """All three parts as one readable line."""
        return f"{self.what} Why: {self.why} What to do: {self.fix}"


_NEW_KEY = (
    "Create a new read-only API key in {vendor} (Developer tools > Authentication) and save it in the vault."
)
_WAIT = (
    "Nothing yet: Yaqzan retries on the next scheduled sync. "
    "If it keeps happening, check {vendor}'s status page."
)
_TELL_CLAUDE = "Report it to Claude with the code; it needs a code fix, not a retry."
_TELL_IF_REPEATS = "Report it to Claude with the code if it repeats."

# code: (what, why, fix)
_CATALOGUE: dict[str, tuple[str, str, str]] = {
    # ---- network: one outbound call
    "net.blocked": (
        "Yaqzan refused to send a request to {vendor}.",
        "The address broke a safety rule (not https, not on the allowlist, or a redirect).",
        "Do not loosen the rule. " + _TELL_CLAUDE,
    ),
    "net.timeout": (
        "{vendor} took too long to answer.",
        "{vendor} is slow or overloaded, or the internet connection is poor.",
        _WAIT,
    ),
    "net.unavailable": (
        "Yaqzan could not reach {vendor}.",
        "No internet connection, a DNS problem, or {vendor} is down.",
        "Check this machine's internet connection. " + _WAIT,
    ),
    "net.auth": (
        "{vendor} refused our API key.",
        "The key has expired, was revoked, or lacks read permission.",
        _NEW_KEY,
    ),
    "net.rate_limited": (
        "{vendor} asked us to slow down.",
        "Too many requests in a short time; we kept getting 'try later' after every retry.",
        _WAIT,
    ),
    "net.server_error": (
        "{vendor} had an error on its side.",
        "{vendor}'s servers failed (a 5xx error) on every retry.",
        _WAIT,
    ),
    "net.client_error": (
        "{vendor} rejected our request.",
        "The request no longer matches what {vendor} expects; its API may have changed.",
        _TELL_CLAUDE,
    ),
    "net.too_large": (
        "{vendor} sent more data than Yaqzan accepts in one answer.",
        "The response was over the size cap, which protects this machine.",
        _TELL_CLAUDE,
    ),
    "net.bad_response": (
        "{vendor} sent an answer Yaqzan could not read.",
        "It was not the JSON we expect; possibly an outage page or an API change.",
        _WAIT,
    ),
    # ---- source: a connector, after the network layer
    "source.unavailable": (
        "{vendor} could not be read just now.",
        "A timeout, a dropped connection or an error on {vendor}'s side.",
        _WAIT,
    ),
    "source.refused": (
        "The request to {vendor} was refused.",
        "A safety rule blocked it, or {vendor} rejected the request.",
        _TELL_CLAUDE,
    ),
    "source.auth": (
        "{vendor} refused our API key.",
        "The key has expired, was revoked, or lacks read permission.",
        _NEW_KEY,
    ),
    "source.rate_limited": (
        "{vendor} asked us to slow down.",
        "Too many requests in a short time.",
        _WAIT,
    ),
    "source.bad_response": (
        "{vendor}'s data broke one of our rules, so none of it was saved.",
        "An answer was unreadable or did not add up (for example, tax larger than the amount).",
        _TELL_CLAUDE,
    ),
    "source.unknown_value": (
        "{vendor} sent a value Yaqzan does not know yet, so none of it was saved.",
        "{vendor} has probably added a new status or type.",
        _TELL_CLAUDE,
    ),
    "source.too_much_data": (
        "{vendor} had more data than one sync may read.",
        "We stop at a page limit so one sync cannot run forever.",
        _TELL_CLAUDE,
    ),
    # ---- store: the database
    "store.unavailable": (
        "Yaqzan could not reach its database.",
        "The database is stopped, restarting or out of resources.",
        "Check that the database is running (on the laptop: uv run python -m tools.dev_db up).",
    ),
    "store.conflict": (
        "The database refused a duplicate record.",
        "The same item was written twice at once.",
        "Usually harmless; the next sync repairs it. " + _TELL_IF_REPEATS,
    ),
    "store.rejected": (
        "The database refused a record that broke one of its rules.",
        "The data failed a safety check stored in the database itself.",
        _TELL_CLAUDE,
    ),
    "store.denied": (
        "The database refused permission.",
        "Yaqzan tried something its database role may not do (by design, e.g. deleting history).",
        _TELL_CLAUDE,
    ),
    "store.failed": (
        "A database operation failed.",
        "An unexpected database error.",
        "Check the database is healthy and has free disk space. " + _TELL_CLAUDE,
    ),
    "store.schema_mismatch": (
        "The database layout does not match this version of Yaqzan.",
        "The database is newer or older than the code, or set up by hand.",
        "Do not start the agents. " + _TELL_CLAUDE,
    ),
    "store.migration_changed": (
        "A past database change has been edited.",
        "Someone altered a migration that already ran; history must never change.",
        "Do not start the agents. " + _TELL_CLAUDE,
    ),
    "store.naive_time": (
        "A time without a timezone reached the database.",
        "A bug in Yaqzan's code.",
        _TELL_CLAUDE,
    ),
    # ---- vault: secrets
    "vault.bad_key_file": (
        "The vault's master key file is unreadable.",
        "It is missing, damaged or in the wrong format.",
        "Restore the master key file from its offline backup. "
        "Do not create a new one: old secrets would be lost.",
    ),
    "vault.key_file_too_open": (
        "The vault refused to use its master key file.",
        "Other users on this machine can read the file.",
        "Restrict the file so only the Yaqzan account can read it.",
    ),
    "vault.unknown_key": (
        "A secret was sealed with a master key this machine does not have.",
        "The key was rotated away, or the secret came from another machine.",
        "Restore the older master key from backup, or save the secret again.",
    ),
    "vault.tampered_or_wrong_slot": (
        "A stored secret failed its integrity check, so it was not used.",
        "The sealed data was altered, or copied to the wrong slot.",
        "Save the secret again. If you did not change anything, treat this as a possible break-in.",
    ),
    "vault.secret_too_large": (
        "A secret is too large to store.",
        "Something that is not a key was pasted in.",
        "Copy just the key and save it again.",
    ),
    "vault.secret_empty": (
        "An empty secret was refused.",
        "Nothing was pasted in.",
        "Copy the key again and save it.",
    ),
    "vault.unsupported_format": (
        "A stored secret is in a format this version cannot read.",
        "It was sealed by a newer or damaged version.",
        "Save the secret again. " + _TELL_IF_REPEATS,
    ),
    # ---- credentials: the key-expiry check and the broker
    "credential.expiring": (
        "The {vendor} API key expires within 14 days.",
        "Vendor keys are issued with an end date; after it, every sync will fail.",
        _NEW_KEY + " Do it before the date shown.",
    ),
    "credential.expired": (
        "The {vendor} API key has expired, so {vendor} numbers will stop updating.",
        "The key passed its end date.",
        _NEW_KEY,
    ),
    "credential.refused": (
        "{vendor} refused our API key on the last sync.",
        "The key has expired, was revoked, or lost read permission.",
        _NEW_KEY,
    ),
    "credential.no_date": (
        "We do not know when the {vendor} API key expires.",
        "No end date was recorded when the key was saved.",
        "Look up the key's end date in {vendor} and save the key again with that date.",
    ),
    "credential.missing": (
        "No {vendor} API key is saved, so {vendor} cannot be read.",
        "The key was never saved for this project, or was removed.",
        _NEW_KEY,
    ),
    # ---- collector, freshness, policy
    "collect.crashed": (
        "The {vendor} sync crashed.",
        "A bug in Yaqzan's code, not an outage.",
        _TELL_CLAUDE,
    ),
    "freshness.stale": (
        "{vendor} numbers are out of date, so they show as unknown.",
        "No successful sync for over twice the schedule.",
        "Look at the last sync error for {vendor}; it says what failed.",
    ),
    "freshness.alert": (
        "{vendor} numbers have been out of date for a long time.",
        "No successful sync for over six times the schedule.",
        "Look at the last sync error for {vendor} and fix it today.",
    ),
    "freshness.never": (
        "{vendor} has never synced, so there are no numbers yet.",
        "The connector is new or has never succeeded.",
        "Check the {vendor} key is saved, then wait for the first sync.",
    ),
    "policy.bad_switch_row": (
        "A kill-switch record is unreadable, so all agents were stopped.",
        "The switch data is damaged; Yaqzan fails safe by stopping everything.",
        _TELL_CLAUDE,
    ),
}

KNOWN_CODES = frozenset(_CATALOGUE)


def explain(code: str, *, vendor: str | None = None) -> Explanation:
    """``code`` in plain English. ``vendor`` is the source name (``paddle`` reads as "Paddle")."""
    name = vendor.capitalize() if vendor else "the service"
    entry = _CATALOGUE.get(code)
    if entry is None:
        return Explanation(
            code,
            "Something failed with a code Yaqzan has no description for.",
            "The code is new or misspelled.",
            _TELL_CLAUDE,
        )
    what, why, fix = (_sentence(part.replace("{vendor}", name)) for part in entry)
    return Explanation(code, what, why, fix)


def _sentence(text: str) -> str:
    """Capitalise the first letter, so "the service is slow" reads as a sentence."""
    return text[:1].upper() + text[1:]
