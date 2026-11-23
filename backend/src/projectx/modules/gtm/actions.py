"""Every action a GTM agent can ask to take, with its safety metadata.

Defaults follow the blueprint's table: publishing anything new asks first; publishing approved
content can earn "Run and tell"; money out (ads, discounts) always asks; automated likes,
follows and mass DMs are never allowed.
"""

from __future__ import annotations

from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level

M = "gtm"

PUBLISH_POST = register_action(
    ActionType(
        key="gtm.publish_post",
        module=M,
        pack="gtm.publisher",
        title="Publish a post",
        description="Publish an approved content variant to a connected platform account.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        public=True,
        undo="Delete the post from the platform.",
    )
)
PUBLISH_MANUAL = register_action(
    ActionType(
        key="gtm.publish_manual",
        module=M,
        pack="gtm.publisher",
        title="Post by hand",
        description="Platforms without API posting (Reddit, unaudited TikTok): the system drafts, a person posts.",
        default_level=Level.DRAFT,
        max_level=Level.DRAFT,
        reversible=True,
        public=True,
    )
)
REPLY_PUBLIC = register_action(
    ActionType(
        key="gtm.reply_public",
        module=M,
        pack="gtm.inbox",
        title="Reply to a comment",
        description="Post a public reply. Only common questions can ever be answered without a person.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        public=True,
        undo="Delete the reply.",
    )
)
REPLY_PRIVATE = register_action(
    ActionType(
        key="gtm.reply_private",
        module=M,
        pack="gtm.inbox",
        title="Reply to a direct message",
        description="Answer a one-to-one message.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Send a correction.",
    )
)
SEND_NEWSLETTER = register_action(
    ActionType(
        key="gtm.send_newsletter",
        module=M,
        pack="gtm.newsletter",
        title="Send a newsletter issue",
        description="Send an issue built from approved content to opted-in subscribers.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        public=True,
        undo="Emails can't be recalled: undo removes the issue from the web archive so you can send a correction.",
    )
)
SEND_SEQUENCE_STEP = register_action(
    ActionType(
        key="gtm.send_sequence_step",
        module=M,
        pack="gtm.nurture",
        title="Send a nurture message",
        description="Send the next step of an approved nurture sequence to an opted-in contact.",
        default_level=Level.RUN_AND_TELL,
        max_level=Level.RUN,
        reversible=True,
        undo="Stop the sequence for this contact and send a correction.",
    )
)
SEND_OUTREACH = register_action(
    ActionType(
        key="gtm.send_outreach",
        module=M,
        pack="gtm.outreach",
        title="Email a new contact",
        description="Send a personal first message to a named prospect.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Stop the sequence and send an apology if needed.",
    )
)
START_CAMPAIGN = register_action(
    ActionType(
        key="gtm.start_campaign",
        module=M,
        pack="gtm.campaigns",
        title="Start a campaign",
        description="Activate a planned campaign: content generation, publishing and budget.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Pause the campaign.",
    )
)
ADJUST_GOAL = register_action(
    ActionType(
        key="gtm.adjust_goal",
        module=M,
        pack="gtm.goals",
        title="Change a goal",
        description="Change a goal's target, deadline, budget or campaign when it falls behind.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=True,
        promotable=False,
    )
)
PUBLISH_PAGE = register_action(
    ActionType(
        key="gtm.publish_landing_page",
        module=M,
        pack="gtm.landing",
        title="Publish a landing page",
        description="Make a campaign landing page public.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        public=True,
        undo="Unpublish the page.",
    )
)
ADOPT_BRAND_BOOK = register_action(
    ActionType(
        key="gtm.adopt_brand_book",
        module=M,
        pack="gtm.brand",
        title="Adopt a brand book",
        description="Make a drafted brand book the voice every piece is written from.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=True,
        promotable=False,
    )
)
UPDATE_PLAYBOOK = register_action(
    ActionType(
        key="gtm.update_playbook",
        module=M,
        pack="gtm.learning",
        title="Ship a learned playbook change",
        description="Weekly learning: change ratios, hooks, themes or posting times based on results.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Revert to the previous playbook version.",
    )
)
OFFER_DISCOUNT = register_action(
    ActionType(
        key="gtm.offer_discount",
        module=M,
        pack="gtm.campaigns",
        title="Offer a discount",
        description="Create a limited-time offer or promo code.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=True,
        moves_money=True,
        promotable=False,
    )
)
CREATE_AD_CAMPAIGN = register_action(
    ActionType(
        key="gtm.create_ad_campaign",
        module=M,
        pack="gtm.ads",
        title="Start paid ads",
        description="Put money behind content that already worked organically.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=False,
        moves_money=True,
    )
)
INCREASE_AD_BUDGET = register_action(
    ActionType(
        key="gtm.increase_ad_budget",
        module=M,
        pack="gtm.ads",
        title="Increase an ad budget",
        description="Any budget increase needs a person.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=False,
        moves_money=True,
    )
)
PAUSE_AD = register_action(
    ActionType(
        key="gtm.pause_ad",
        module=M,
        pack="gtm.ads",
        title="Pause an ad or creative",
        description="Pause ads whose cost per lead stays above the limit, or switch off losing creatives.",
        default_level=Level.RUN_AND_TELL,
        max_level=Level.RUN,
        reversible=True,
        undo="Resume the ad.",
    )
)
PAUSE_ACCOUNT = register_action(
    ActionType(
        key="gtm.pause_account",
        module=M,
        pack="gtm.safety",
        title="Pause posting on an account",
        description="Safety trigger: a platform warning, spam flag or spike in complaints pauses the account.",
        default_level=Level.RUN_AND_TELL,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Resume posting on the account.",
        promotable=False,
    )
)
AUTO_ENGAGE = register_action(
    ActionType(
        key="gtm.auto_engage",
        module=M,
        pack="gtm.safety",
        title="Automated likes, follows or mass DMs",
        description="Never automated: it breaks platform rules and gets accounts banned.",
        default_level=Level.NEVER,
        max_level=Level.NEVER,
        reversible=False,
        promotable=False,
    )
)
