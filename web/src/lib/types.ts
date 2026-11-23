/** API shapes used by the cockpit. They mirror the backend views (snake_case on purpose). */

export interface User {
  id: string;
  email: string;
  name: string;
  email_verified: boolean;
  is_staff: boolean;
}

export interface Business {
  id: string;
  name: string;
  slug: string;
  website: string | null;
  industry: string | null;
  business_model: string;
  description: string | null;
  country: string | null;
  timezone: string;
  currency: string;
  plan: string;
  status: string;
  practice_mode: boolean;
  onboarding: Record<string, unknown>;
  settings: Record<string, unknown>;
  role?: string;
}

export interface Me {
  user: User | null;
  businesses: Business[];
  csrf_token: string | null;
}

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number;
}

export type Level = "never" | "draft" | "ask_first" | "run_and_tell" | "run";

export interface Decision {
  id: string;
  action_type: string;
  kind: "approval" | "manual_task" | "auto" | "promotion" | "choice";
  status: string;
  level: Level;
  level_label: string;
  title: string;
  summary: string;
  why_now: string | null;
  forecast: Record<string, number | string> | null;
  cost_minor: number | null;
  currency: string | null;
  if_no: string | null;
  undo: string | null;
  payload: Record<string, unknown>;
  preview: Record<string, unknown> | null;
  options: { key: string; label: string; description?: string }[] | null;
  reasons: { code: string; message: string; cap: Level }[];
  subject_type: string | null;
  subject_id: string | null;
  priority: number;
  expires_at: string | null;
  requested_by: string;
  decided_at: string | null;
  reason_code: string | null;
  note: string | null;
  edited: boolean;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  can_undo: boolean;
  created_at: string;
}

export interface BriefingItem {
  module: string;
  kind: string;
  title: string;
  body: string;
  level: "info" | "success" | "warning" | "critical";
  link: string | null;
  data: Record<string, unknown>;
}

export interface Cockpit {
  business: { id: string; name: string; timezone: string; currency: string; practice_mode: boolean };
  user: { name: string };
  numbers: {
    revenue?: { value_minor: number; currency: string; display: string; previous_minor: number };
    churn?: { rate: number };
    customers?: number;
    new_leads_30d?: number;
    new_subscribers_30d?: number;
    new_customers_30d?: number;
  };
  briefing: BriefingItem[];
  decisions: { count: number; items: Decision[] };
}

export interface Notification {
  id: string;
  kind: string;
  level: string;
  title: string;
  body: string;
  link: string | null;
  created_at: string;
  read: boolean;
}

export interface Goal {
  id: string;
  type: string;
  title: string;
  metric: string;
  metric_label: string;
  unit: string;
  baseline_value: number;
  target_value: number;
  current_value: number;
  start_date: string;
  deadline: string;
  budget_minor: number;
  currency: string;
  status: string;
  growth_approach: string;
  approval_mode: string;
  forecast: Forecast | null;
  pace: { actual: number; expected_to_date: number; projected: number; status: string; days_left: number } | null;
  higher_is_better: boolean;
  pace_history?: { at: string; actual: number; expected_to_date: number; projected: number; status: string }[];
  restrictions?: Restriction[];
  campaigns?: Campaign[];
}

export interface Forecast {
  p10: number;
  p50: number;
  p90: number;
  probability: number;
  verdict: "likely" | "stretch" | "unrealistic" | "achieved";
  days: number;
  daily_rate_p50: number;
  breakdown: Record<string, number>;
  assumptions: string[];
  suggestions: { kind: string; label: string; days?: number; target?: number; budget_minor?: number }[];
  metric?: string;
}

export interface Restriction {
  id: string;
  kind: string;
  label: string;
  params: Record<string, unknown>;
  active: boolean;
}

export interface Playbook {
  key: string;
  name: string;
  category: string;
  goals: string[];
  choose_when: string;
  strategies: string[];
  runs: "ai" | "ai_human" | "human";
  tier: string;
  channels: string[];
  content_mix: Record<string, number>;
  frequency: Record<string, number>;
  value_ratio: number;
  metrics: string[];
  steps: string[];
  paid: boolean;
  approval_note?: string | null;
}

export interface Ranked {
  playbook: Playbook;
  score: number;
  reasons: string[];
  blocked: string | null;
}

export interface Campaign {
  id: string;
  goal_id: string | null;
  playbook_key: string;
  playbook_name: string;
  category: string | null;
  runs: string | null;
  role: string;
  name: string;
  status: string;
  starts_on: string | null;
  ends_on: string | null;
  budget_minor: number;
  channels: string[];
  frequency: Record<string, number>;
  value_ratio: number;
  brief: string;
  created_at: string;
}

export interface Variant {
  id: string;
  platform: string;
  platform_label: string;
  format: string;
  format_label: string;
  title: string | null;
  text: string;
  thread: string[];
  media: { url?: string; key?: string; kind?: string }[];
  spec: { brand?: Check; specs?: Check; ai_label?: string };
  status: string;
  scheduled_at: string | null;
}

export interface Check {
  name: string;
  passed: boolean;
  severity: "block" | "review" | "info";
  issues: string[];
  score: number | null;
}

export interface ContentItem {
  id: string;
  campaign_id: string | null;
  pillar: string;
  intent: "value" | "ask";
  hook_type: string | null;
  title: string;
  core_idea: string;
  body: string;
  cta: string | null;
  source_type: string;
  source_ref: Record<string, unknown>;
  status: string;
  checks: { brand?: Check; claims?: Check; duplicates?: Check; outcome?: string };
  planned_for: string | null;
  decision_id: string | null;
  variants?: Variant[];
  posts?: Post[];
}

export interface Post {
  id: string;
  platform: string;
  status: string;
  url: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  verified_at: string | null;
  last_error: string | null;
  metrics: Record<string, number>;
  score: number | null;
}

export interface PlatformInfo {
  key: string;
  label: string;
  kind: string;
  formats: { key: string; label: string }[];
  api_posting: string;
  daily_post_limit: number | null;
  approval_notes: string;
  best_for: string;
  oauth: boolean;
  app_configured: boolean;
  accounts: Account[];
}

export interface Account {
  id: string;
  platform: string;
  name: string;
  handle: string | null;
  status: string;
  followers: number | null;
  last_error: string | null;
  settings: Record<string, unknown>;
}

export interface Contact {
  id: string;
  email: string | null;
  name: string | null;
  company: string | null;
  stage: string;
  side_state: string | null;
  status: string;
  source: string | null;
  lead_score: number;
  first_touch: { platform?: string; campaign_id?: string } | null;
  tags: string[];
  unsubscribed: boolean;
  last_activity_at: string | null;
  lifetime_value_minor: number;
  created_at: string;
  handles: Record<string, string>;
  next_action: string | null;
}

export interface Funnel {
  stages: {
    stage: string;
    count: number;
    reached: number;
    conversion_to_next: number | null;
    median_days_in_stage: number | null;
  }[];
  side_states: Record<string, number>;
  total: number;
}

export interface InboxItem {
  id: string;
  platform: string;
  kind: string;
  author_handle: string | null;
  author_name: string | null;
  text: string;
  received_at: string;
  sentiment: number | null;
  category: string | null;
  status: string;
  ai_draft: string | null;
  reply_text: string | null;
  contact_id: string | null;
}

export interface Usage {
  plan: { key: string; name: string; price_usd_month: number };
  metrics: Record<string, { used: number; limit: number | null }>;
  compute: {
    spent_today_micro_usd: number;
    cap_micro_usd: number | null;
    costliest_tasks: { task: string; calls: number; cost_micro_usd: number; cached: number }[];
    steps_by_rung: Record<string, number>;
  };
  models: Record<string, { provider: string; model: string; available: boolean }>;
}

export interface AutonomyRow {
  action_type: string;
  module: string;
  title: string;
  description: string;
  level: Level;
  level_label: string;
  max_level: Level;
  one_way_door: boolean;
  moves_money: boolean;
  public: boolean;
  clean_streak: number;
  threshold: number;
  total_runs: number;
  promotion_pending: boolean;
}
