"""Build the iOS shortcut «Send til Mediafang»: in the share sheet (YouTube app, Safari, ...) it
asks Video or Bare lyd, posts the link to /api/hent, and shows mediafang's answer as a
notification. Run without a link from the home screen, it asks for one.

The API sits behind a Cloudflare Access service token, which the shortcut carries:

    uv run python tools/make_shortcut.py --client-id ... --client-secret ... -o unsigned.shortcut
    shortcuts sign --mode anyone --input unsigned.shortcut --output "Send til Mediafang.shortcut"

The result holds the token: keep it out of git, AirDrop it to the phone.
"""

import argparse
import plistlib
import uuid
from pathlib import Path

OBJ = "￼"  # where a variable sits in a text field


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def output(uuid_: str, name: str) -> dict:
    return {"OutputUUID": uuid_, "Type": "ActionOutput", "OutputName": name}


def attachment(value: dict) -> dict:
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def text(s: str, var: dict | None = None) -> dict:
    """A text field: plain, or (when var is given) just that variable."""
    if var is None:
        return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}
    return {"Value": {"string": OBJ, "attachmentsByRange": {"{0, 1}": var}}, "WFSerializationType": "WFTextTokenString"}


def dictionary(items: dict[str, dict]) -> dict:
    return {
        "Value": {"WFDictionaryFieldValueItems": [
            {"WFItemType": 0, "WFKey": text(k), "WFValue": v} for k, v in items.items()
        ]},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def build(url: str, client_id: str, client_secret: str) -> dict:
    links, menu, response, message = _uuid(), _uuid(), _uuid(), _uuid()
    group = _uuid()
    actions = [
        action("is.workflow.actions.detect.link", UUID=links, WFInput=attachment({"Type": "ExtensionInput"})),
        action("is.workflow.actions.choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=0,
               WFMenuPrompt="Hent som", WFMenuItems=["Video", "Bare lyd"]),
        action("is.workflow.actions.choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=1,
               WFMenuItemTitle="Video"),
        action("is.workflow.actions.gettext", WFTextActionText="video"),
        action("is.workflow.actions.choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=1,
               WFMenuItemTitle="Bare lyd"),
        action("is.workflow.actions.gettext", WFTextActionText="audio"),
        action("is.workflow.actions.choosefrommenu", GroupingIdentifier=group, WFControlFlowMode=2, UUID=menu),
        action(
            "is.workflow.actions.downloadurl", UUID=response, WFURL=url, WFHTTPMethod="POST", WFHTTPBodyType="JSON",
            ShowHeaders=True,
            WFHTTPHeaders=dictionary({"CF-Access-Client-Id": text(client_id),
                                      "CF-Access-Client-Secret": text(client_secret)}),
            WFJSONValues=dictionary({"url": text("", output(links, "URLs")),
                                     "type": text("", output(menu, "Menu Result"))}),
        ),
        action("is.workflow.actions.getvalueforkey", UUID=message, WFDictionaryKey="message",
               WFGetDictionaryValueType="Value", WFInput=attachment(output(response, "Contents of URL"))),
        action("is.workflow.actions.notification", WFNotificationActionTitle="Mediafang",
               WFNotificationActionBody=text("", output(message, "Dictionary Value"))),
    ]
    return {
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFSafariWebPageContentItem", "WFStringContentItem"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorAskForInput",
                                      "Parameters": {"ItemClass": "WFURLContentItem"}},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowActions": actions,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="https://media.flagan.net/api/hent")
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", required=True)
    p.add_argument("-o", "--output", type=Path, default=Path("unsigned.shortcut"))
    args = p.parse_args()
    args.output.write_bytes(plistlib.dumps(build(args.url, args.client_id, args.client_secret), fmt=plistlib.FMT_BINARY))
    print(f"Wrote {args.output}; now: shortcuts sign --mode anyone --input {args.output} --output 'Send til Mediafang.shortcut'")


if __name__ == "__main__":
    main()
