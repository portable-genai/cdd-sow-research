"""The half of the provenance contract that lives in the BROWSER.

Every served console states, at the top of every page, WHERE it is running and WHICH model
answers (org decision, 2026-08-30). The SERVICE half of that contract -- the profile implies a
runtime, the schema carries both fields, the endpoint answers from the binding the container
builds rather than from a literal -- is pinned in
``tests/unit/test_health_provenance.py`` and is not restated here.

This file pins the other half, and the other half is the one that broke. On 2026-09-04 eight
consoles were found to have been rendering NOTHING on every page load since the banner landed:
the component named ``/api/agent``, the same-origin route handler the service template ships,
in trees that ship no such handler. The health call reached a path nothing serves, took the
failure branch, and the failure branch renders nothing -- deliberately, because a strip that
guessed would assert provenance it does not have. A check that cannot fail loudly fails as an
ABSENCE, and an absent strip is exactly what no reviewer notices.

Every service-side assertion was true and green throughout. That is why these live in their own
file: a green service half says nothing about whether a reader ever sees the sentence.

The assertions pin AGREEMENT rather than literals, because this fleet legitimately runs the
console in more than one shape and a check keyed on one of them passes by blindness in the
others -- which is how a working strip in nine consoles came to be reported as missing.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path("ui")

#: The sentence the strip renders, which is spelled once in whichever component owns it.
#:
#: Locating that component by what it SAYS rather than by where it sits is the point. This fleet
#: keeps the strip in three different places: ``ui/app/ProvenanceBanner.tsx`` in the trees that
#: took the 2026-08-31 sweep, ``ui/components/ProvenanceBanner.tsx`` in the launch-set consoles
#: that adopted it a day earlier, and inline in the app chrome in ``cdd-sow-research``, which
#: carried it before either. A path-keyed check reports two of those three as having no banner
#: at all, and a class-keyed one (``.provenance-banner``) misses every console that styles the
#: strip with utility classes instead. Both mistakes have been made.
_WORDING = "running on GCP"

#: Build output and vendored packages are not this console's source.
_NOT_SOURCE = frozenset({"node_modules", ".next", "dist", "out", "coverage"})


def _console_sources() -> list[Path]:
    """Every ``.tsx`` this console actually ships, build output and vendored trees pruned."""
    found: list[Path] = []
    pending = [UI]
    while pending:
        for child in pending.pop().iterdir():
            if child.is_dir():
                if child.name not in _NOT_SOURCE:
                    pending.append(child)
            elif child.suffix == ".tsx":
                found.append(child)
    return sorted(found)


def _banner_source() -> Path:
    """The component that renders the strip, wherever this console keeps it."""
    hits = [p for p in _console_sources() if _WORDING in p.read_text()]
    assert hits, (
        "no component under ui/ renders the provenance wording, so this console states neither "
        "its runtime nor its model at the top of any page (org decision, 2026-08-30)"
    )
    assert len(hits) == 1, (
        f"more than one component renders the strip ({[str(p) for p in hits]}), so two pages can "
        "phrase the same fact differently and only one of them can be the one a screenshot "
        "came from"
    )
    return hits[0]


def test_the_strip_is_mounted_in_the_layout_rather_than_in_a_page() -> None:
    """Being at the top of EVERY page is a property of the console, not of any page.

    Mounted per page, the strip is present on the pages somebody remembered and absent on the
    one a screenshot came from -- and the absence is invisible, because the strip renders
    nothing until the service answers anyway. The layout is the only mount that cannot be
    forgotten by adding a route.
    """
    layout = Path("ui/app/layout.tsx")
    assert layout.is_file(), "this console has no root layout, so nothing can be mounted for it"
    owner = _banner_source().stem
    assert owner in layout.read_text(), (
        f"{owner} renders the provenance strip but the root layout does not reference it, so "
        "the strip reaches only the pages that remember to mount it"
    )


def test_the_banner_calls_a_base_this_console_actually_serves() -> None:
    """The defect that shipped, stated as an assertion.

    Both architectures are legitimate, so this pins AGREEMENT rather than a literal. A tree with
    ``ui/app/api/agent`` proxies through its own origin and the strip should name that path; a
    tree without one must reach its backend the way the rest of the console does, through the
    ``NEXT_PUBLIC_*`` base resolved once in ``ui/lib/api``. The combination that shipped --
    naming the proxy while having none -- is the only one that is never right.

    Sharing the base is what makes the health call REACHABLE rather than merely tidy: the
    ``connect-src`` the console ships is built from that same value, and a cross-origin
    standalone run is on the service's CORS allowlist because every other call already needs to
    be. A health check on a base of its own would have to earn both of those separately, and
    would be silently refused until it did.
    """
    source = _banner_source()
    banner = source.read_text()
    proxies_through_own_origin = Path("ui/app/api/agent").is_dir()

    assert ('"/api/agent"' in banner) == proxies_through_own_origin, (
        f"{source} names /api/agent but this console has no route handler at ui/app/api/agent, "
        "so the health call reaches nothing and the strip renders nothing"
        if not proxies_through_own_origin
        else f"this console ships a /api/agent route handler but {source} does not use it"
    )

    if not proxies_through_own_origin:
        # Either spelling of the same fact: the component may import the resolved base itself,
        # or call the client function that already closes over it. What it must not do is spell
        # a base of its own -- a second, independently resolved origin is how the two drift
        # apart again, and the drift is invisible until a deployment serves through a proxy.
        reaches_the_shared_client = "API_BASE" in banner or re.search(
            r'from\s+"(?:\.\./)+lib/api(?:\.mjs)?"', banner
        )
        assert reaches_the_shared_client, (
            f"{source} must reach its backend through the base the rest of this console reads "
            "(ui/lib/api, which resolves NEXT_PUBLIC_*) rather than spelling one of its own"
        )


def _rule(css: str, selector: str) -> str | None:
    """The body of one CSS rule, or ``None`` when this stylesheet does not carry it."""
    opening = f"{selector} {{"
    if opening not in css:
        return None
    start = css.index(opening)
    return css[start : css.index("}", start)]


def _first_px(block: str, property_name: str) -> int:
    """The TOP value of a ``margin``/``padding`` shorthand, in px, or 0 if unset.

    Only the shorthand is read because that is the only form these stylesheets use. A parser
    that quietly returned 0 for a longhand it could not see would make the assertion below pass
    by blindness, so an unreadable value fails here instead.
    """
    match = re.search(rf"^\s*{property_name}:\s*([^;]+);", block, re.MULTILINE)
    if match is None:
        return 0
    first = match.group(1).split()[0]
    assert first.endswith("px") or first == "0", (
        f"{property_name} shorthand starts with {first!r}, which this check cannot read; "
        "express the offset in px so the geometry stays assertable"
    )
    return int(first.removesuffix("px"))


#: Utility classes that move an element UP. Tailwind spells a negative offset with a leading
#: dash, so this is the whole vocabulary that can hoist the strip out of the viewport.
_PULLS_UP = ("-mt-", "-my-", "-top-", "-inset-y-", "-inset-")


def test_the_strip_is_where_a_reader_can_actually_see_it() -> None:
    """A strip that renders off-screen has satisfied every other assertion in this file.

    The strip is full-bleed, and the two console shapes reach the viewport edge differently.

    A plain-CSS console cancels the padding its host carries: ``margin: -32px -18px 20px``
    against a ``body`` with ``padding: 32px 18px``. That is correct, and correct ONLY where
    there is something to cancel. Carried into a console whose ``body`` has no padding, the same
    three numbers hoist the strip 32px ABOVE the viewport: it renders, it holds the right text,
    it is in the DOM on every page load, and it is visible on none. That happened, in eight
    trees, alongside the fetch defect above -- neither half alone would have put the strip on
    the page.

    A utility-class console reaches the edge by not being inset in the first place: its ``body``
    carries no padding and the strip is a plain block. There is no cancellation to get wrong,
    so what this asserts there is that none was introduced.

    Either way the property is the same one: whatever the strip pulls itself up by, the host
    must carry at least that much padding to give back.
    """
    css_path = Path("ui/app/globals.css")
    assert css_path.is_file(), "this console has no root stylesheet"
    css = css_path.read_text()
    styled_rule = _rule(css, ".provenance-banner")

    if styled_rule is None:
        # The utility-class shape. The offsets live on the element, so read them there.
        banner = _banner_source().read_text()
        offenders = sorted(
            {
                token
                for token in re.findall(r"[-\w:./\[\]%]+", banner)
                if token.startswith(_PULLS_UP)
            }
        )
        assert not offenders, (
            f"the strip carries {offenders}, which pulls it up out of a layout that never "
            "inset it: this console's body has no padding to give back, so the strip renders "
            "above the viewport and no reader sees the provenance it exists to state"
        )
        return

    pulled_up = _first_px(styled_rule, "margin")
    given_back = _first_px(_rule(css, "body") or "", "padding")
    assert pulled_up + given_back >= 0, (
        f"the strip pulls itself up {-pulled_up}px to cancel padding, but body gives back only "
        f"{given_back}px, so it renders {-(pulled_up + given_back)}px above the viewport and no "
        "reader ever sees the provenance this file exists to guarantee"
    )
