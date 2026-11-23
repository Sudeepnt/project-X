"""GTM tables. Every table is tenant-scoped (row-level security) unless noted."""

from projectx.modules.gtm.models.ads import AdAccount, AdCampaign, AdMetricDaily
from projectx.modules.gtm.models.brand import Audience, BrandBook
from projectx.modules.gtm.models.channels import ChannelAccount, Post, PostMetricSnapshot
from projectx.modules.gtm.models.compliance import DataRequest
from projectx.modules.gtm.models.content import Asset, ContentFingerprint, ContentItem, ContentTheme, ContentVariant
from projectx.modules.gtm.models.engagement import CommunityDaily, CommunityMember, InboxItem
from projectx.modules.gtm.models.growth import LandingPage, LandingVariant, LinkClick, PageEvent, PromoCode, TrackedLink
from projectx.modules.gtm.models.learning import Experiment, ExperimentVariant, PatternStat, Report
from projectx.modules.gtm.models.nurture import Message, NewsletterIssue, Sequence, SequenceEnrollment
from projectx.modules.gtm.models.strategy import Campaign, Goal, Restriction

__all__ = [
    "AdAccount",
    "AdCampaign",
    "AdMetricDaily",
    "Asset",
    "Audience",
    "BrandBook",
    "Campaign",
    "ChannelAccount",
    "CommunityDaily",
    "CommunityMember",
    "ContentFingerprint",
    "ContentItem",
    "ContentTheme",
    "ContentVariant",
    "DataRequest",
    "Experiment",
    "ExperimentVariant",
    "Goal",
    "InboxItem",
    "LandingPage",
    "LandingVariant",
    "LinkClick",
    "Message",
    "NewsletterIssue",
    "PageEvent",
    "PatternStat",
    "Post",
    "PostMetricSnapshot",
    "PromoCode",
    "Report",
    "Restriction",
    "Sequence",
    "SequenceEnrollment",
    "TrackedLink",
]
