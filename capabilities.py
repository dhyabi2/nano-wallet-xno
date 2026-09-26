"""The capability document an outside agent shows its operator.

One source of truth: `tools_registered` and `tools_absent` are derived
from profiles.PROFILE_TOOLS, the same tuple the MCP server registers
from. The document therefore cannot claim a tool set the server does not
serve - and the test suite asserts the two against each other rather
than against a constant, so a future edit to one side fails the build.

`operator_scope_sentence` is the field that matters. It is the exact
text an agent pastes to its human for the one-time approval, and it is
FIXED IN CODE, not templated per recipient: a scope sentence that varies
per conversation is a scope sentence the operator has to read twice, and
the second reading is where the approval is lost.
"""

import json

import profiles

VERSION = "1"

WELL_KNOWN_PATH = "/.well-known/nano-receive-only"

OPERATOR_SCOPE_SENTENCE = (
    "This agent may generate a Nano (XNO) receiving address and accept incoming "
    "payments. It cannot send, spend, or sign outgoing transfers. No key leaves "
    "this machine."
)

CAN = [
    "generate a Nano address inside your own process, offline",
    "validate any Nano address, offline",
    "read a balance",
    "pocket incoming XNO (receive)",
]

CANNOT = [
    "send XNO",
    "sign a spend of any kind",
    "transmit, export or display your private key or seed",
    "reach the network at all for address creation or validation",
]

CUSTODY = (
    "non-custodial by construction: the keypair is generated in your process and "
    "never transmitted. No party other than you can spend from the address."
)


class UnknownProfile(ValueError):
    pass


def document(profile: str = profiles.RECEIVE_ONLY) -> dict:
    if profile not in profiles.PROFILE_TOOLS:
        raise UnknownProfile(
            "unknown profile %r; known profiles: %s"
            % (profile, ", ".join(profiles.PROFILE_NAMES))
        )
    registered = list(profiles.tools_for(profile))
    absent = [tool for tool in sorted(profiles.SPEND_TOOLS) if tool not in registered]
    return {
        "profile": profile,
        "version": VERSION,
        "can": list(CAN),
        "cannot": list(CANNOT),
        "custody": CUSTODY,
        "tools_registered": registered,
        "tools_absent": absent,
        "verify_yourself": "nano-wallet selfcheck --profile %s" % profile,
        "operator_scope_sentence": OPERATOR_SCOPE_SENTENCE,
        "spec": "specs/agent-tool-receive-only-onboarding.md",
    }


def as_json(profile: str = profiles.RECEIVE_ONLY) -> str:
    return json.dumps(document(profile), indent=2, sort_keys=True)
