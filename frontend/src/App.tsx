import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  AlertCircle,
  ArrowUp,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CirclePlay,
  CircleDashed,
  CircleStop,
  ClipboardList,
  Clock3,
  Copy,
  Download,
  ExternalLink,
  FileText,
  Github,
  Globe2,
  Home,
  Library,
  LockKeyhole,
  LogIn,
  LogOut,
  Loader2,
  PanelRightOpen,
  Plus,
  RefreshCw,
  Repeat2,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import axios from "axios";

axios.defaults.withCredentials = true;

// ============================================================
// Types
// ============================================================

type RunStatusName = "queued" | "running" | "completed" | "failed" | "stopped";
type FocusView = "brief" | "sources" | "evidence" | "claims" | "report";
type NodeState = "done" | "active" | "failed" | "";

const DEFAULT_ARTIFACT_PANE_WIDTH = 500;
const MIN_ARTIFACT_PANE_WIDTH = 360;
const MAX_ARTIFACT_PANE_WIDTH = 820;
const DESKTOP_LAYOUT_BREAKPOINT = 1080;
const DESKTOP_LEFT_RAIL_WIDTH = 272;
const RESIZE_HANDLE_WIDTH = 4;
const MIN_CONVERSATION_WIDTH = 380;

function clampArtifactPaneWidth(width: number, viewportWidth = window.innerWidth) {
  if (viewportWidth <= DESKTOP_LAYOUT_BREAKPOINT) return DEFAULT_ARTIFACT_PANE_WIDTH;
  const maxForViewport = viewportWidth - DESKTOP_LEFT_RAIL_WIDTH - RESIZE_HANDLE_WIDTH - MIN_CONVERSATION_WIDTH;
  const responsiveMax = Math.max(MIN_ARTIFACT_PANE_WIDTH, Math.min(MAX_ARTIFACT_PANE_WIDTH, maxForViewport));
  return Math.max(MIN_ARTIFACT_PANE_WIDTH, Math.min(responsiveMax, width));
}

interface ResearchRequest {
  project_name: string;
  target_product: string;
  product_description: string;
  competitors: string[];
  analysis_dimensions: string[];
  research_goal: string;
  seed_urls: string[] | string;
  max_sources: number;
  max_sources_per_query: number;
  auto_discover_sources: boolean;
  max_search_rounds: number;
}

interface RunMetrics {
  source_candidates: number;
  sources_fetched: number;
  sources_failed: number;
  evidence_count: number;
  claim_count: number;
  verified_claim_count: number;
  challenged_claim_count: number;
  matrix_cell_count?: number;
  recommendation_count?: number;
  battlecard_count?: number;
  average_evidence_confidence?: number;
  coverage_score?: number;
}

interface RunStatus {
  run_id: string;
  project_name: string;
  target_product: string;
  status: RunStatusName;
  current_stage: string;
  started_at: string;
  finished_at?: string | null;
  metrics: RunMetrics;
  node_status: Record<string, string>;
  warnings: string[];
  error?: string | null;
}

interface SourceCandidate {
  url: string;
  title: string;
  snippet: string;
  content?: string;
  content_source?: string;
  source_type: string;
  query: string;
  score: number;
  source_provider?: string;
}

interface EvidenceSummary {
  evidence_id: string;
  dimension: string;
  dimension_label: string;
  competitor?: string | null;
  fact: string;
  quote_preview: string;
  source_title: string;
  source_url: string;
  source_type: string;
  confidence: number;
  fetched_at: string;
}

interface Claim {
  claim_id: string;
  dimension: string;
  dimension_label: string;
  claim: string;
  supporting_evidence_ids: string[];
  counter_evidence_ids: string[];
  confidence: number;
  risk_level: "low" | "medium" | "high";
  reasoning_summary: string;
  verification_status: string;
  red_team_notes: Array<{
    risk_type: string;
    comment: string;
    suggested_action: string;
    severity: "low" | "medium" | "high";
  }>;
  final_wording?: string | null;
}

interface TraceEvent {
  event_id: string;
  timestamp: string;
  node: string;
  phase: string;
  status: string;
  message: string;
  payload?: Record<string, unknown>;
}

interface SourceTask {
  task_id: string;
  entity: string;
  dimension: string;
  intent: string;
  query: string;
  expected_source_types: string[];
  rationale: string;
}

interface ResearchPlan {
  research_goal: string;
  competitors: string[];
  dimensions: string[];
  queries: string[];
  source_tasks: SourceTask[];
  required_agents: string[];
  quality_rules: string[];
  notes: string;
  planned_by: string;
}

interface MatrixCell {
  competitor: string;
  dimension: string;
  dimension_label: string;
  summary: string;
  evidence_count: number;
  confidence: number;
  status: "strong" | "partial" | "weak" | "unknown";
}

interface CompetitorMatrix {
  competitors: string[];
  dimensions: string[];
  dimension_labels: Record<string, string>;
  cells: MatrixCell[];
  coverage_by_competitor: Record<string, number>;
}

interface OpportunityRecommendation {
  recommendation_id: string;
  title: string;
  recommendation: string;
  priority: "low" | "medium" | "high";
  target_audience: string;
  rationale: string;
  expected_value: string;
  next_steps: string[];
  confidence: number;
}

interface BattlecardItem {
  item_id: string;
  competitor: string;
  customer_scenario: string;
  competitor_strength: string;
  our_response: string;
  talk_track: string;
  confidence: number;
}

interface ObservabilitySnapshot {
  total_duration_seconds: number;
  evidence_coverage_score: number;
  claim_pass_rate: number;
  report_confidence: number;
  dimension_coverage: Record<string, number>;
  competitor_coverage: Record<string, number>;
}

interface RunDetail {
  status: RunStatus;
  request: ResearchRequest;
  plan?: ResearchPlan | null;
  sources: SourceCandidate[];
  evidence: EvidenceSummary[];
  claims: Claim[];
  trace: TraceEvent[];
  matrix?: CompetitorMatrix | null;
  recommendations?: OpportunityRecommendation[];
  battlecards?: BattlecardItem[];
  observability?: ObservabilitySnapshot | null;
  report_markdown: string;
  executive_summary_markdown?: string | null;
  methodology_markdown?: string | null;
  report_path?: string | null;
}

interface Capabilities {
  llm_configured: boolean;
  llm_provider: string;
  llm_model: string;
  search_provider: string;
  search_providers?: string[];
}

interface SessionState {
  authenticated: boolean;
  username?: string | null;
}

interface DraftState {
  projectName: string;
  target: string;
  productDescription: string;
  competitors: string;
  goal: string;
  seedUrls: string;
  dimensions: string[];
  maxSources: number;
  maxSourcesPerQuery: number;
  maxSearchRounds: number;
  showAdvanced: boolean;
}

// ============================================================
// Constants
// ============================================================

const stageOrder = [
  "ResearchPlanningAgent",
  "SourceResearchAgent",
  "EvidenceStructuringAgent",
  "AnalysisAndReviewAgent",
  "ReportComposerAgent",
];

interface AgentMeta {
  Icon: typeof ClipboardList;
  label: string;
  shortLabel: string;
  color: string;
  glowRgb: string;
}

const AGENT_META: Record<string, AgentMeta> = {
  ResearchPlanningAgent: { Icon: ClipboardList, label: "Research Planning", shortLabel: "Plan", color: "#334155", glowRgb: "51,65,85" },
  SourceResearchAgent:   { Icon: Search,        label: "Source Research",   shortLabel: "Search", color: "#2563eb", glowRgb: "37,99,235" },
  EvidenceStructuringAgent: { Icon: Library,    label: "Evidence",          shortLabel: "Evidence", color: "#0f766e", glowRgb: "15,118,110" },
  AnalysisAndReviewAgent:   { Icon: ShieldCheck, label: "Analysis & Review", shortLabel: "Analyze", color: "#b7791f", glowRgb: "183,121,31" },
  ReportComposerAgent:      { Icon: FileText,   label: "Report Composer",   shortLabel: "Report", color: "#0d9488", glowRgb: "13,148,136" },
};

const dimensionOptions = [
  { label: "定位", value: "positioning" },
  { label: "功能", value: "feature" },
  { label: "定价", value: "pricing" },
  { label: "口碑", value: "user_voice" },
  { label: "企业", value: "enterprise" },
  { label: "战略", value: "strategy" },
];

const viewOptions: Array<{ key: FocusView; label: string; icon: typeof Sparkles }> = [
  { key: "brief",    label: "Brief",    icon: Sparkles },
  { key: "sources",  label: "Sources",  icon: Globe2 },
  { key: "evidence", label: "Evidence", icon: Library },
  { key: "claims",   label: "Claims",   icon: CheckCircle2 },
  { key: "report",   label: "Report",   icon: FileText },
];

// ============================================================
// App
// ============================================================

export default function App() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [runs, setRuns] = useState<RunStatus[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [, setHealthOk] = useState<boolean | null>(null);
  const [view, setView] = useState<FocusView>("report");
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [draft, setDraft] = useState<DraftState>(defaultDraft());

  const activeStatus = detail?.status ?? runs.find((r) => r.run_id === activeRunId) ?? null;
  const activeTrace = detail?.trace ?? [];
  const isRunning = activeStatus?.status === "running" || activeStatus?.status === "queued";
  const viewTabsRef = useRef<HTMLDivElement>(null);

  useEffect(() => { void checkSession(); }, []);

  useEffect(() => {
    if (session?.authenticated) void refreshAll();
  }, [session?.authenticated]);

  useEffect(() => {
    if (session?.authenticated && activeRunId) void refreshDetail(activeRunId);
  }, [activeRunId, session?.authenticated]);

  useEffect(() => {
    if (!session?.authenticated) return;
    const interval = window.setInterval(() => {
      void refreshRuns();
      if (activeRunId) void refreshDetail(activeRunId, true);
      if (!isRunning) void refreshCapabilities();
    }, isRunning ? 2600 : 9000);
    return () => window.clearInterval(interval);
  }, [activeRunId, isRunning, session?.authenticated]);

  useEffect(() => {
    const activeButton = viewTabsRef.current?.querySelector<HTMLButtonElement>("button.active");
    activeButton?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [view]);

  async function checkSession() {
    try {
      const r = await axios.get<SessionState>("/api/me");
      setSession(r.data);
    } catch {
      setSession({ authenticated: false });
    }
  }

  async function login(username: string, password: string) {
    const r = await axios.post<SessionState>("/api/login", { username, password });
    setSession(r.data);
  }

  async function logout() {
    try {
      await axios.post("/api/logout");
    } finally {
      setSession({ authenticated: false });
      setRuns([]);
      setActiveRunId(null);
      setDetail(null);
      setCapabilities(null);
      setComposerOpen(false);
    }
  }

  async function refreshAll() {
    await Promise.all([refreshRuns(), refreshCapabilities()]);
  }

  async function refreshRuns() {
    try {
      const [health, runResponse] = await Promise.all([
        axios.get("/health"),
        axios.get<RunStatus[]>("/api/runs"),
      ]);
      setHealthOk(health.data.status === "ok");
      setRuns(runResponse.data);
      if (activeRunId && !runResponse.data.some((run) => run.run_id === activeRunId)) {
        setActiveRunId(null);
        setDetail(null);
      }
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        setSession({ authenticated: false });
      }
      setHealthOk(false);
    }
  }

  async function refreshCapabilities() {
    try {
      const r = await axios.get<Capabilities>("/api/capabilities");
      setCapabilities(r.data);
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        setSession({ authenticated: false });
      }
      setCapabilities(null);
    }
  }

  async function refreshDetail(runId = activeRunId, silent = false) {
    if (!runId) return;
    if (!silent) setRefreshing(true);
    try {
      const r = await axios.get<RunDetail>(`/api/runs/${runId}`);
      setDetail(r.data);
      setHealthOk(true);
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        setSession({ authenticated: false });
      }
      setHealthOk(false);
    } finally {
      if (!silent) setRefreshing(false);
    }
  }

  async function startRun() {
    setSubmitting(true);
    try {
      const payload: ResearchRequest = {
        project_name: draft.projectName.trim() || "Competitive Research",
        target_product: draft.target.trim() || "Trae",
        product_description: draft.productDescription.trim(),
        competitors: normalizeList(draft.competitors),
        analysis_dimensions: draft.dimensions,
        research_goal: draft.goal.trim(),
        seed_urls: normalizeLines(draft.seedUrls),
        max_sources: draft.maxSources,
        max_sources_per_query: draft.maxSourcesPerQuery,
        auto_discover_sources: true,
        max_search_rounds: draft.maxSearchRounds,
      };
      const r = await axios.post<{ run_id: string }>("/api/runs", payload);
      setActiveRunId(r.data.run_id);
      setDetail(null);
      setView("report");
      setComposerOpen(false);
      await refreshRuns();
    } finally {
      setSubmitting(false);
    }
  }

  async function deleteRun(runId: string) {
    if (!window.confirm("Delete this research run? This cannot be undone.")) return;
    try {
      await axios.delete(`/api/runs/${runId}`);
      const remaining = runs.filter((r) => r.run_id !== runId);
      setRuns(remaining);
      if (activeRunId === runId) {
        setActiveRunId(remaining[0]?.run_id ?? null);
        setDetail(null);
      }
    } catch (e) {
      console.error("Delete failed", e);
    }
  }

  async function stopRun(runId = activeRunId) {
    if (!runId) return;
    setStopping(true);
    // Optimistically mark as stopped locally so UI reflects immediately
    setDetail((prev) =>
      prev ? { ...prev, status: { ...prev.status, status: "stopped", current_stage: "Stopped" } } : null
    );
    try {
      await axios.post(`/api/runs/${runId}/stop`);
      await Promise.all([refreshRuns(), refreshDetail(runId, true)]);
    } finally {
      setStopping(false);
    }
  }

  const shellRef = useRef<HTMLElement>(null);
  const [rightWidth, setRightWidth] = useState(DEFAULT_ARTIFACT_PANE_WIDTH);
  const isResizing = useRef(false);

  useEffect(() => {
    const onResize = () => setRightWidth((width) => clampArtifactPaneWidth(width));
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  function handleResizeStart(e: React.MouseEvent) {
    e.preventDefault();
    isResizing.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const onMove = (ev: MouseEvent) => {
      if (!isResizing.current || !shellRef.current) return;
      const rect = shellRef.current.getBoundingClientRect();
      const newWidth = rect.right - ev.clientX;
      setRightWidth(clampArtifactPaneWidth(newWidth, rect.width));
    };
    const onUp = () => {
      isResizing.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  if (session === null) {
    return <div className="auth-loading"><Loader2 size={18} className="spin" /> Checking session</div>;
  }

  if (!session.authenticated) {
    return <LoginPage onLogin={login} />;
  }

  return (
    <main ref={shellRef} className="product-shell">
      {/* ─── Left rail ───────────────────────────────── */}
      <aside className="left-rail">
        <button
          className="home-icon-btn"
          onClick={() => { setActiveRunId(null); setDetail(null); }}
          title="Home"
        >
          <Home size={18} />
        </button>

        <div className="brand-copy">
          <strong>CompeteInsight</strong>
          <span>Research OS · {session.username}</span>
        </div>

        <button className="new-thread" onClick={() => setComposerOpen(true)}>
          <Plus size={17} />
          New research
        </button>

        <div className="rail-section">
          <div className="rail-label">Recent · {runs.length}</div>
          <div className="run-stack">
            {runs.slice(0, 30).map((run) => (
              <RunRow
                key={run.run_id}
                run={run}
                active={run.run_id === activeRunId}
                onSelect={() => { setDetail(null); setActiveRunId(run.run_id); setView("report"); }}
                onDelete={() => void deleteRun(run.run_id)}
              />
            ))}
            {!runs.length && <p className="empty-note">No research runs yet.</p>}
          </div>
        </div>

        <div className="rail-footer">
          <span title={capabilities?.search_provider ?? "checking"}>
            <Search size={14} />
            {capabilities?.search_provider ?? "checking"}
          </span>
          <button className="rail-logout" onClick={() => void logout()} title="Log out">
            <LogOut size={14} />
            Log out
          </button>
        </div>
      </aside>

      {/* ─── Conversation pane ───────────────────────── */}
      <section className="conversation-pane">
        <header className="top-strip">
          <div>
            <span className="eyebrow">Deep research workspace</span>
            <h1>{activeStatus?.project_name ?? "Ask for a market map"}</h1>
          </div>
          <div className="top-actions">
            {activeStatus && (activeStatus.status === "running" || activeStatus.status === "queued") && (
              <button className="stop-button" onClick={() => void stopRun()} disabled={stopping}>
                <CircleStop size={16} />
                {stopping ? "Stopping" : "Stop"}
              </button>
            )}
            <button className="ghost-button" onClick={() => void refreshDetail()}>
              <RefreshCw size={16} className={refreshing ? "spin" : ""} />
              Refresh
            </button>
          </div>
        </header>

      {!activeStatus ? (
        <div className="thread-scroll landing-scroll">
          <section className="hero-composer">
            <div className="hero-copy">
              <span className="spark-chip"><Sparkles size={14} /> Evidence-first intelligence</span>
              <h2>Turn a rough competitor question into a source-backed research brief.</h2>
              <p>CompeteInsight plans the search, collects public sources, extracts evidence, challenges claims, and keeps every artifact auditable.</p>
            </div>
            <IntroCarousel />
            <HeroResourceStrip />
          </section>
        </div>
      ) : (
        <>
          <div className="run-header-bar">
            <RunHeader status={activeStatus} detail={detail} />
          </div>
          <div className="event-log-window">
            <EventTimeline
              events={activeTrace}
              plan={detail?.plan}
              sources={detail?.sources ?? []}
            />
          </div>
          <div className="pipeline-bar">
            <CompactPipeline status={activeStatus} plan={detail?.plan} />
            {activeStatus.warnings.length > 0 && (
              <div className="soft-warning">
                <AlertCircle size={15} />
                {activeStatus.warnings[0]}
              </div>
            )}
          </div>
        </>
      )}

    </section>

      {/* ─── Resize handle ───────────────────────────── */}
      <div className="resize-handle" onMouseDown={handleResizeStart} />

      {/* ─── Artifact pane ───────────────────────────── */}
      <aside className="artifact-pane" style={{ width: rightWidth }}>
        <div className="artifact-head">
          <div>
            <span className="eyebrow">Artifacts</span>
            <strong>{activeStatus?.target_product ?? "No active run"}</strong>
          </div>
          <PanelRightOpen size={18} />
        </div>

        <div className="view-switch-wrap">
          <div className="view-switch" ref={viewTabsRef}>
            {viewOptions.map((item) => {
              const Icon = item.icon;
              return (
                <button key={item.key} className={view === item.key ? "active" : ""} onClick={() => setView(item.key)}>
                  <Icon size={15} />
                  {item.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className={`artifact-content ${view === "report" ? "report-active" : ""}`}>
          <ArtifactView detail={detail} view={view} />
        </div>
      </aside>

      {/* ─── Composer modal ──────────────────────────── */}
      <AnimatePresence>
        {composerOpen && (
          <motion.div
            className="composer-overlay"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={() => setComposerOpen(false)}
          >
            <motion.div
              className="composer-modal"
              initial={{ opacity: 0, y: 24, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 16, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 320, damping: 28 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-header">
                <div>
                  <h2>New Research</h2>
                  <p>Define what you want to investigate, then hit Launch.</p>
                </div>
                <button className="modal-close" onClick={() => setComposerOpen(false)}>
                  <X size={18} />
                </button>
              </div>
              <ResearchComposer draft={draft} setDraft={setDraft} submitting={submitting} onSubmit={() => void startRun()} />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}

function LoginPage({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await onLogin(username.trim(), password);
    } catch {
      setError("Username or password is incorrect.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-showcase" aria-label="CompeteInsight overview">
        <div className="login-copy">
          <span className="spark-chip"><Sparkles size={14} /> Evidence-first intelligence</span>
          <h1>CompeteInsight</h1>
          <p>Turn rough competitor questions into source-backed research briefs, auditable claims, and follow-up analysis.</p>
        </div>
        <IntroCarousel variant="login" />
      </section>

      <section className="login-panel" aria-label="Sign in">
        <div className="login-panel-inner">
          <div className="login-lock">
            <LockKeyhole size={18} />
          </div>
          <div>
            <span className="eyebrow">Private demo access</span>
            <h2>Sign in to the research workspace</h2>
            <p>Runs and reports stay isolated under your session on the CompeteInsight server.</p>
          </div>

          <form className="login-form" onSubmit={(e) => void submit(e)}>
            <label>
              <span>Username</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                spellCheck={false}
              />
            </label>
            <label>
              <span>Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                autoFocus
              />
            </label>
            {error && <p className="login-error">{error}</p>}
            <button className="login-submit" type="submit" disabled={loading || !username.trim() || !password}>
              {loading ? <Loader2 size={16} className="spin" /> : <LogIn size={16} />}
              {loading ? "Signing in" : "Sign in"}
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}

const introSlides: [
  { src: string; title: string; caption: string },
  ...Array<{ src: string; title: string; caption: string }>
] = [
  {
    src: "/intro.png",
    title: "Launch Research",
    caption: "输入目标产品、竞品和研究目标，一次启动完整的竞品研究链路。",
  },
  {
    src: "/intro1.png",
    title: "Search",
    caption: "批量 Query 快速获取公开信息，再由 Search LLM 针对缺口补充来源。",
  },
  {
    src: "/intro2.png",
    title: "Evidence",
    caption: "从已采集正文中抽取可追溯事实，保留来源、原文片段和置信度。",
  },
  {
    src: "/intro3.png",
    title: "Claims",
    caption: "把 Evidence 聚合为可审查的 Claim，并通过 Red Team 降低过度推断风险。",
  },
  {
    src: "/intro4.png",
    title: "Report",
    caption: "生成带引用的竞品报告、矩阵和方法说明，方便直接审阅和导出。",
  },
  {
    src: "/intro5.png",
    title: "AI Analysis Assistant",
    caption: "基于本次 research 的证据和报告继续追问，快速定位关键结论。",
  },
];

function IntroCarousel({ variant = "landing" }: { variant?: "landing" | "login" }) {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setIndex((value) => (value + 1) % introSlides.length);
    }, 4200);
    return () => window.clearInterval(timer);
  }, []);

  const slide = introSlides[index] ?? introSlides[0];

  return (
    <section className={`intro-showcase ${variant === "login" ? "login-intro" : ""}`} aria-label="Product overview">
      <div className="intro-frame">
        <AnimatePresence mode="wait">
          <motion.img
            key={slide.src}
            src={slide.src}
            alt={slide.title}
            initial={{ opacity: 0, x: 28, scale: 0.985 }}
            animate={{ opacity: 1, x: 0, scale: 1 }}
            exit={{ opacity: 0, x: -28, scale: 0.985 }}
            transition={{ duration: 0.45, ease: "easeOut" }}
          />
        </AnimatePresence>
      </div>
      <div className="intro-caption">
        <div>
          <strong>{slide.title}</strong>
          <span>{slide.caption}</span>
        </div>
        <div className="intro-dots">
          {introSlides.map((item, itemIndex) => (
            <button
              key={item.src}
              type="button"
              className={itemIndex === index ? "active" : ""}
              aria-label={`Show ${item.title}`}
              onClick={() => setIndex(itemIndex)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

function HeroResourceStrip() {
  return (
    <div className="hero-resource-strip" aria-label="Project resources">
      <span>Explore the project, watch a demo.</span>
      <div>
        <a href="https://github.com/example/competegraph" target="_blank" rel="noreferrer">
          <Github size={15} />
          GitHub
        </a>
        <a href="https://example.com/demo" target="_blank" rel="noreferrer">
          <CirclePlay size={15} />
          Watch demo
        </a>
      </div>
    </div>
  );
}

// ============================================================
// RunRow
// ============================================================

function RunRow({ run, active, onSelect, onDelete }: {
  run: RunStatus; active: boolean; onSelect: () => void; onDelete: () => void;
}) {
  return (
    <div className={`run-row ${active ? "active" : ""}`} onClick={onSelect}>
      <span className={`status-dot ${run.status}`} />
      <span className="run-row-text">
        <strong>{run.project_name}</strong>
        <small>{run.target_product} · {formatShortDate(run.started_at)}</small>
      </span>
      <button
        className="run-row-delete"
        title="Delete run"
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}

// ============================================================
// RunHeader  — compact topic card at top of conversation
// ============================================================

function RunHeader({ status, detail }: { status: RunStatus; detail: RunDetail | null }) {
  const progress = runProgress(status);
  return (
    <div className="run-header-card">
      <div className="run-header-top">
        <RunBadge status={status.status} />
        <span className="run-header-title">{status.project_name}</span>
      </div>
      <div className="run-header-info">
        <span className="rh-target">{status.target_product}</span>
        {detail?.request.competitors.length ? (
          <span className="rh-vs">vs {detail.request.competitors.join(" · ")}</span>
        ) : null}
      </div>
      {detail?.request.research_goal && (
        <p className="rh-goal">{detail.request.research_goal}</p>
      )}
      <div className="rh-progress-row">
        <div className="rh-progress-track">
          <motion.span
            className="rh-progress-fill"
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.5, ease: "easeOut" }}
          />
        </div>
        <span className="rh-progress-pct">{progress}%</span>
      </div>
    </div>
  );
}

// ============================================================
// EventTimeline  — full scrollable event log (center main)
// ============================================================

function EventTimeline({ events, plan, sources }: {
  events: TraceEvent[];
  plan?: ResearchPlan | null;
  sources: SourceCandidate[];
}) {
  const [expanded, setExpanded] = useState(false);
  const MAX_COLLAPSED = 15;

  if (!events.length) {
    return (
      <div className="trace-empty">
        <Clock3 size={15} />
        <span>Waiting for the first event…</span>
      </div>
    );
  }

  const hidden = events.length - MAX_COLLAPSED;
  const displayed = expanded ? events : events.slice(-MAX_COLLAPSED);

  return (
    <div className="event-timeline">
      <div className="trace-header">
        <span className="trace-title">
          <Clock3 size={13} />
          Event log · {events.length}
        </span>
        {events.length > MAX_COLLAPSED && (
          <button className="trace-expand-btn" onClick={() => setExpanded((p) => !p)}>
            {expanded ? "Show recent" : `Show all ${events.length}`}
            <ChevronDown size={12} style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
          </button>
        )}
      </div>

      {!expanded && hidden > 0 && (
        <div className="trace-ellipsis">↑ {hidden} earlier events hidden</div>
      )}

      <div className="trace-list">
        <AnimatePresence initial={false}>
          {displayed.map((event) => (
            <motion.div
              key={event.event_id}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.18 }}
            >
              <TraceItem event={event} sources={sources} plan={plan} />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

function TraceItem({ event, sources, plan }: {
  event: TraceEvent;
  sources: SourceCandidate[];
  plan?: ResearchPlan | null;
}) {
  const [open, setOpen] = useState(false);
  const [messageOpen, setMessageOpen] = useState(false);
  const meta = AGENT_META[normalizeStage(event.node)];
  const isError = event.phase === "error";
  const isComplete = event.phase === "complete";

  const payload = event.payload ?? {};
  const query = payload.query as string | undefined;
  const url = payload.url as string | undefined;
  const count = payload.count as number | undefined;
  const traceMessage = formatTraceMessage(event);
  const isLongMessage = traceMessage.length > 150;
  const displayMessage = isLongMessage && !messageOpen ? `${traceMessage.slice(0, 150)}...` : traceMessage;

  // Cross-reference URL with sources for snippet
  const matchedSource = url ? sources.find((s) => s.url === url) : undefined;

  // Plan queries expansion after planning complete
  const isPlanComplete = isComplete && normalizeStage(event.node) === "ResearchPlanningAgent" && plan;

  const extraPayload = Object.entries(payload).filter(([k]) => !["query", "url", "count"].includes(k));
  const hasExtra = extraPayload.length > 0 && !isPlanComplete;

  return (
    <div className={`trace-item ${isError ? "error" : isComplete ? "complete" : ""}`}>
      <div className="trace-item-left">
        <span className="trace-dot" style={{
          background: isError ? "var(--red)" : isComplete ? "var(--green)" : (meta?.color ?? "var(--faint)"),
        }} />
      </div>
      <div className="trace-item-body">
        <div className="trace-item-top">
          <span className="trace-agent" style={{ color: meta?.color ?? "var(--muted)" }}>
            {meta?.shortLabel ?? agentLabel(event.node)}
          </span>
          <span className="trace-msg">
            {displayMessage}
            {isLongMessage && (
              <button className="trace-inline-expand" type="button" onClick={() => setMessageOpen((p) => !p)}>
                {messageOpen ? "收起" : "查看完整"}
              </button>
            )}
          </span>
          <time className="trace-time">{formatTime(event.timestamp)}</time>
        </div>

        {query && (
          <div className="trace-query">
            <Search size={11} />
            <code>{query}</code>
          </div>
        )}

        {url && (
          <div className="trace-url">
            <Globe2 size={11} />
            <a href={url} target="_blank" rel="noreferrer">{truncateUrl(url)}</a>
            {matchedSource?.title && <span className="trace-url-title">— {matchedSource.title}</span>}
          </div>
        )}

        {matchedSource?.snippet && (
          <div className="trace-snippet">"{matchedSource.snippet.slice(0, 140)}"</div>
        )}

        {count !== undefined && count > 0 && (
          <span className="trace-count">{count} items</span>
        )}

        {/* Plan queries expansion */}
        {isPlanComplete && plan!.source_tasks.length > 0 && (
          <div className="trace-plan-queries">
            <button className="trace-payload-toggle" onClick={() => setOpen((p) => !p)}>
              <Search size={11} />
              查看 Planning 查询
              <ChevronDown size={11} style={{ transform: open ? "rotate(180deg)" : "none", transition: "0.15s" }} />
            </button>
            {open && (
              <div className="trace-queries-list">
                {plan!.source_tasks.map((task) => (
                  <div key={task.task_id} className="trace-query-item">
                    <span className="tqi-entity">{task.entity}</span>
                    <code className="tqi-query">{task.query}</code>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Generic payload detail */}
        {hasExtra && (
          <>
            <button className="trace-payload-toggle" onClick={() => setOpen((p) => !p)}>
              Details
              <ChevronDown size={11} style={{ transform: open ? "rotate(180deg)" : "none", transition: "0.15s" }} />
            </button>
            {open && <PayloadDetail payload={payload} />}
          </>
        )}
      </div>
    </div>
  );
}

function PayloadDetail({ payload }: { payload: Record<string, unknown> }) {
  const skip = new Set(["query", "url", "count"]);
  const entries = Object.entries(payload).filter(([k]) => !skip.has(k));
  if (!entries.length) return null;

  // Array (e.g., search results)
  const arrEntry = entries.find(([, v]) => Array.isArray(v));
  if (arrEntry) {
    const [key, arr] = arrEntry;
    const items = arr as Record<string, unknown>[];
    return (
      <div className="payload-detail">
        <div className="payload-label">{key} ({items.length})</div>
        {items.slice(0, 12).map((item, i) => (
          <div key={i} className="payload-result-item">
            {item.title != null && <strong>{String(item.title)}</strong>}
            {item.url != null && (
              <a href={String(item.url)} target="_blank" rel="noreferrer" className="payload-url">
                <Globe2 size={11} />
                {truncateUrl(String(item.url))}
              </a>
            )}
            {(item.snippet ?? item.excerpt) != null && (
              <p>{String(item.snippet ?? item.excerpt).slice(0, 160)}</p>
            )}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="payload-detail">
      {entries.map(([k, v]) => (
        <div key={k} className="payload-kv">
          <span className="payload-key">{k}</span>
          <span className="payload-val">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        </div>
      ))}
    </div>
  );
}

// ============================================================
// CompactPipeline  — small pipeline bar at bottom
// ============================================================

function CompactPipeline({ status, plan }: { status: RunStatus; plan?: ResearchPlan | null }) {
  const progress = runProgress(status);
  return (
    <div className="compact-pipeline">
      <div className="cp-header">
        <Zap size={12} />
        <span>Agent pipeline</span>
        <span className="cp-loop-badge"><Repeat2 size={11} /> research loop</span>
        <span className="cp-pct">{progress}% complete</span>
      </div>
      <div className="cp-nodes">
        {stageOrder.map((stage, i) => {
          const state = stageState(status, stage) as NodeState;
          const meta = AGENT_META[stage];
          if (!meta) return null;
          const isDone = state === "done";
          const isActive = state === "active";
          const isFailed = state === "failed";

          return (
            <Fragment key={stage}>
              <div
                className={`cp-node ${state || "pending"}`}
                style={{ "--agent-color": meta.color, "--glow-color": `rgba(${meta.glowRgb},0.18)` } as React.CSSProperties}
                title={isDone ? getNodeStats(status, stage, plan) : meta.label}
              >
                <div className="cp-icon-wrap">
                  {isDone ? <CheckCircle2 size={13} style={{ color: "var(--green)" }} />
                  : isFailed ? <XCircle size={13} style={{ color: "var(--red)" }} />
                  : isActive ? <meta.Icon size={13} style={{ color: meta.color }} />
                  : <meta.Icon size={13} style={{ color: "var(--faint)" }} />}
                </div>
                <span className="cp-label" style={{ color: isActive ? meta.color : isDone ? "var(--ink)" : "var(--faint)" }}>
                  {meta.shortLabel}
                </span>
                <span className="cp-sub">
                  {isDone ? getNodeStats(status, stage, plan)
                  : isActive ? "Working…"
                  : isFailed ? "Failed"
                  : "—"}
                </span>
              </div>
              {i < stageOrder.length - 1 && (
                <div
                  className={`cp-conn ${isDone ? "done" : isActive ? "active" : ""}`}
                  style={{ "--agent-color": meta.color } as React.CSSProperties}
                >
                  <ChevronRight size={11} />
                </div>
              )}
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}

function getNodeStats(status: RunStatus, stage: string, plan?: ResearchPlan | null): string {
  const m = status.metrics;
  switch (stage) {
    case "ResearchPlanningAgent": {
      void plan;
      return "Plan ready";
    }
    case "SourceResearchAgent":
      return m.source_candidates ? `${m.source_candidates} sources` : "Searching";
    case "EvidenceStructuringAgent":
      return m.evidence_count ? `${m.evidence_count} items` : "Done";
    case "AnalysisAndReviewAgent":
      return m.claim_count ? `${m.claim_count} claims` : "Done";
    case "ReportComposerAgent":
      return "Report ready";
    default:
      return "Done";
  }
}

// ============================================================
// ResearchComposer
// ============================================================

function ResearchComposer({ draft, setDraft, submitting, onSubmit }: {
  draft: DraftState; setDraft: (v: DraftState) => void; submitting: boolean; onSubmit: () => void;
}) {
  const [suggesting, setSuggesting] = useState(false);
  const [suggestedChips, setSuggestedChips] = useState<Array<{ name: string; reason: string }>>([]);
  const [suggestError, setSuggestError] = useState("");

  async function suggestCompetitors() {
    if (!draft.target.trim()) return;
    setSuggesting(true);
    setSuggestError("");
    try {
      const r = await axios.post<{ competitors: Array<{ name: string; reason: string }>; llm_configured: boolean }>(
        "/api/suggest_competitors",
        { product_name: draft.target.trim(), product_description: draft.productDescription.trim() },
      );
      if (!r.data.llm_configured) {
        setSuggestError("LLM 未配置，使用规则推荐（配置 API Key 可获得更精准建议）");
      }
      setSuggestedChips(r.data.competitors);
    } catch {
      setSuggestError("推荐失败，请手动填写竞品");
    } finally {
      setSuggesting(false);
    }
  }

  function adoptSuggestion(name: string) {
    const existing = normalizeList(draft.competitors);
    if (!existing.includes(name)) {
      setDraft({ ...draft, competitors: [...existing, name].join(", ") });
    }
  }

  function removeSuggestion(name: string) {
    const updated = normalizeList(draft.competitors).filter((c) => c !== name);
    setDraft({ ...draft, competitors: updated.join(", ") });
  }

  const currentCompetitors = normalizeList(draft.competitors);

  return (
    <div className="composer-card">
      <textarea
        className="composer-textarea"
        value={draft.goal}
        onChange={(e) => setDraft({ ...draft, goal: e.target.value })}
        placeholder="研究目标：例如「对比 Trae 与 Cursor、GitHub Copilot、Windsurf 在定价、功能和企业化方面的差异」"
        rows={3}
      />

      {/* Target + Description row */}
      <div className="composer-target-row">
        <label className="composer-label-block">
          <span>目标产品</span>
          <input
            value={draft.target}
            onChange={(e) => setDraft({ ...draft, target: e.target.value })}
            placeholder="Trae"
          />
        </label>
        <label className="composer-label-block composer-desc-label">
          <span>产品描述（帮助识别同名产品）</span>
          <input
            value={draft.productDescription}
            onChange={(e) => setDraft({ ...draft, productDescription: e.target.value })}
            placeholder="一个 AI 编程助手 / AI coding tool"
          />
        </label>
        <label className="composer-label-block composer-name-label">
          <span>项目名</span>
          <input
            value={draft.projectName}
            onChange={(e) => setDraft({ ...draft, projectName: e.target.value })}
            placeholder="AI coding assistant landscape"
          />
        </label>
      </div>

      {/* Competitors row */}
      <div className="composer-competitors-section">
        <div className="composer-competitors-header">
          <span className="composer-field-label">竞品（逗号分隔）</span>
          <button
            className={`suggest-btn ${suggesting ? "loading" : ""}`}
            type="button"
            onClick={() => void suggestCompetitors()}
            disabled={suggesting || !draft.target.trim()}
          >
            {suggesting ? <Loader2 size={13} className="spin" /> : <Sparkles size={13} />}
            {suggesting ? "推荐中…" : "推荐竞品"}
          </button>
        </div>
        <input
          className="competitors-input"
          value={draft.competitors}
          onChange={(e) => setDraft({ ...draft, competitors: e.target.value })}
          placeholder="Cursor, GitHub Copilot, Windsurf"
        />
        {suggestError && <p className="suggest-error">{suggestError}</p>}
        {suggestedChips.length > 0 && (
          <div className="suggested-chips">
            <span className="chips-label">推荐竞品：</span>
            {suggestedChips.map((c) => {
              const adopted = currentCompetitors.includes(c.name);
              return (
                <button
                  key={c.name}
                  className={`suggest-chip ${adopted ? "adopted" : ""}`}
                  type="button"
                  title={c.reason}
                  onClick={() => adopted ? removeSuggestion(c.name) : adoptSuggestion(c.name)}
                >
                  {adopted ? <CheckCircle2 size={12} /> : <Plus size={12} />}
                  {c.name}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Dimension tags */}
      <div className="dimension-row">
        {dimensionOptions.map((dim) => (
          <button
            key={dim.value}
            type="button"
            className={draft.dimensions.includes(dim.value) ? "selected" : ""}
            onClick={() => {
              const dims = draft.dimensions.includes(dim.value)
                ? draft.dimensions.filter((d) => d !== dim.value)
                : [...draft.dimensions, dim.value];
              setDraft({ ...draft, dimensions: dims });
            }}
          >
            {dim.label}
          </button>
        ))}
      </div>

      {/* Advanced toggle */}
      <button
        className="advanced-toggle"
        type="button"
        onClick={() => setDraft({ ...draft, showAdvanced: !draft.showAdvanced })}
      >
        <Settings2 size={14} />
        高级设置
        <ChevronDown size={13} style={{ transform: draft.showAdvanced ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
      </button>

      {draft.showAdvanced && (
        <div className="advanced-panel">
          <div className="advanced-grid">
            <label>
              <span>最大来源数</span>
              <input type="number" min={5} max={1000} value={draft.maxSources} onChange={(e) => setDraft({ ...draft, maxSources: Number(e.target.value) || 150 })} />
            </label>
            <label>
              <span>每次查询结果数</span>
              <input type="number" min={1} max={8} value={draft.maxSourcesPerQuery} onChange={(e) => setDraft({ ...draft, maxSourcesPerQuery: Number(e.target.value) || 3 })} />
            </label>
            <label>
              <span>搜索最大轮次（1-5）</span>
              <input type="number" min={1} max={5} value={draft.maxSearchRounds} onChange={(e) => setDraft({ ...draft, maxSearchRounds: Number(e.target.value) || 3 })} />
            </label>
          </div>
          <label>
            <span>种子 URL（每行一个，优先抓取）</span>
            <textarea rows={3} value={draft.seedUrls} onChange={(e) => setDraft({ ...draft, seedUrls: e.target.value })} placeholder="https://example.com/pricing" />
          </label>
        </div>
      )}

      <div className="composer-footer">
        <span className="composer-hint-text">
          {draft.dimensions.length} 维度 · 最多 {draft.maxSources} 来源 · {draft.maxSearchRounds} 轮搜索
        </span>
        <button className="launch-button" onClick={onSubmit} disabled={submitting || !draft.goal.trim()}>
          {submitting ? <Loader2 size={16} className="spin" /> : <ArrowUp size={16} />}
          {submitting ? "启动中…" : "启动研究"}
        </button>
      </div>
    </div>
  );
}

// ============================================================
// ArtifactView
// ============================================================

function ArtifactView({ detail, view }: { detail: RunDetail | null; view: FocusView }) {
  if (!detail) {
    return (
      <div className="artifact-empty">
        <FileText size={24} />
        <p>Research artifacts will appear here once a run is active.</p>
      </div>
    );
  }
  if (view === "sources")  return <SourcesView sources={detail.sources} />;
  if (view === "evidence") return <EvidenceView evidence={detail.evidence} />;
  if (view === "claims")   return <ClaimsView claims={detail.claims} />;
  if (view === "report")   return <ReportView detail={detail} />;
  return <BriefView detail={detail} />;
}

// ── Brief ──────────────────────────────────────────────────

function BriefView({ detail }: { detail: RunDetail }) {
  const m = detail.status.metrics;
  const obs = detail.observability;

  return (
    <div className="brief-view-wrap">
      {/* Scrollable content */}
      <div className="brief-scroll">
        <div className="brief-hero">
          <span className="brief-tag">{detail.request.target_product}</span>
          <p className="brief-goal">{detail.request.research_goal}</p>
        </div>

        {detail.request.competitors.length > 0 && (
          <div className="artifact-section">
            <div className="section-label">Competitors</div>
            <div className="chip-cloud">
              {detail.request.competitors.map((c) => <span key={c} className="chip">{c}</span>)}
            </div>
          </div>
        )}

        <div className="artifact-section">
          <div className="section-label">Coverage</div>
          <div className="metrics-grid-5">
            <MiniStat label="Sources"  value={m.source_candidates} />
            <MiniStat label="Evidence" value={m.evidence_count} />
            <MiniStat label="Claims"   value={m.claim_count} />
            <MiniStat label="Verified" value={m.verified_claim_count} />
            <MiniStat label="Coverage" value={`${Math.round((m.coverage_score ?? 0) * 100)}%`} />
          </div>
        </div>

        {obs && (
          <div className="artifact-section">
            <div className="section-label">Quality signals</div>
            <div className="quality-grid">
              <QualityBar label="Evidence coverage" value={obs.evidence_coverage_score} />
              <QualityBar label="Claim pass rate"   value={obs.claim_pass_rate} />
              <QualityBar label="Report confidence" value={obs.report_confidence} />
            </div>
            {obs.total_duration_seconds > 0 && (
              <span className="obs-duration"><Clock3 size={12} /> Completed in {obs.total_duration_seconds.toFixed(1)}s</span>
            )}
          </div>
        )}

        {detail.request.analysis_dimensions.length > 0 && (
          <div className="artifact-section">
            <div className="section-label">Analysis dimensions</div>
            <div className="chip-cloud">
              {detail.request.analysis_dimensions.map((d) => (
                <span key={d} className="chip dim-chip">{dimensionLabel(d)}</span>
              ))}
            </div>
          </div>
        )}

        {detail.matrix && detail.matrix.cells.length > 0 && (
          <div className="artifact-section">
            <div className="section-label">Competitive matrix · {detail.matrix.cells.length} cells</div>
            <div className="matrix-preview">
              <table>
                <thead>
                  <tr>
                    <th />
                    {detail.matrix.competitors.slice(0, 4).map((c) => <th key={c}>{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {detail.matrix.dimensions.slice(0, 5).map((dim) => (
                    <tr key={dim}>
                      <td className="matrix-dim">{detail.matrix!.dimension_labels[dim] ?? dim}</td>
                      {detail.matrix!.competitors.slice(0, 4).map((comp) => {
                        const cell = detail.matrix!.cells.find((c) => c.competitor === comp && c.dimension === dim);
                        return (
                          <td key={comp} className={`matrix-cell ${cell?.status ?? "unknown"}`} title={cell?.summary}>
                            {cellIcon(cell?.status)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

      </div>
    </div>
  );
}

function QualityBar({ label, value }: { label: string; value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "var(--green)" : pct >= 40 ? "var(--amber)" : "var(--red)";
  return (
    <div className="quality-bar-item">
      <div className="quality-bar-top">
        <span>{label}</span>
        <span style={{ color }}>{pct}%</span>
      </div>
      <div className="quality-bar-track">
        <div className="quality-bar-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

function cellIcon(status?: "strong" | "partial" | "weak" | "unknown") {
  if (status === "strong")  return "✓";
  if (status === "partial") return "~";
  if (status === "weak")    return "✗";
  return "·";
}

// ── ChatPanel ─────────────────────────────────────────────

function ChatPanel({ runId, disabled }: { runId: string; disabled?: boolean }) {
  const [messages, setMessages] = useState<{ id: string; role: "user" | "assistant"; content: string }[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, loading]);

  async function send() {
    if (!input.trim() || loading) return;
    const msg = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { id: Date.now().toString(), role: "user", content: msg }]);
    setLoading(true);
    try {
      const r = await axios.post<{ response: string }>(`/api/runs/${runId}/chat`, { message: msg });
      setMessages((prev) => [...prev, { id: Date.now().toString() + "a", role: "assistant", content: r.data.response }]);
    } catch {
      setMessages((prev) => [...prev, { id: Date.now().toString() + "e", role: "assistant", content: "Failed to get a response. Please check if the LLM API key is configured." }]);
    } finally {
      setLoading(false);
    }
  }

  const suggestions = ["Key differentiators?", "Pricing comparison summary", "Main risks identified", "Opportunity gaps?"];

  return (
    <div className={`chat-panel${disabled ? " chat-panel-disabled" : ""}`}>
      <div className="chat-messages" ref={scrollRef}>
        {disabled && (
          <div className="chat-empty">
            <Bot size={18} />
            <p>The AI assistant is available after the report is generated.</p>
          </div>
        )}
        {!disabled && messages.length === 0 && (
          <div className="chat-empty">
            <Bot size={18} />
            <p>Ask about this research. Examples:</p>
            <div className="chat-suggestions">
              {suggestions.map((s) => (
                <button key={s} className="chat-suggestion" onClick={() => setInput(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`chat-msg ${m.role}`}>
            {m.role === "assistant" ? <MarkdownContent content={m.content} /> : <span>{m.content}</span>}
          </div>
        ))}
        {loading && (
          <div className="chat-msg assistant">
            <span className="chat-typing"><span /><span /><span /></span>
          </div>
        )}
      </div>
      <div className="chat-input-row">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }}
          placeholder={disabled ? "Report required before chatting…" : "Ask about this research…"}
          disabled={loading || disabled}
        />
        <button className="chat-send-btn" onClick={() => void send()} disabled={!input.trim() || loading || disabled}>
          {loading ? <Loader2 size={14} className="spin" /> : <ArrowUp size={14} />}
        </button>
      </div>
    </div>
  );
}

// ── Sources ───────────────────────────────────────────────

function SourcesView({ sources }: { sources: SourceCandidate[] }) {
  const [filterType, setFilterType] = useState("all");
  const types = ["all", ...Array.from(new Set(sources.map((s) => s.source_type))).filter(Boolean)];
  const filtered = filterType === "all" ? sources : sources.filter((s) => s.source_type === filterType);
  const sorted = [...filtered].sort((a, b) => b.score - a.score);

  return (
    <div className="artifact-scroll">
      {sources.length > 0 && (
        <>
          <div className="source-summary">
            <strong>{sources.length}</strong>
            <span>discovered sources</span>
          </div>
          <div className="filter-bar">
            {types.map((t) => (
              <button key={t} className={filterType === t ? "active" : ""} onClick={() => setFilterType(t)}>{t}</button>
            ))}
          </div>
        </>
      )}
      {sorted.map((s) => (
        <a className="source-item" href={s.url} target="_blank" rel="noreferrer" key={s.url + s.query}>
          <div className="source-item-top">
            <span className="source-type-tag">{s.source_type}</span>
            {s.source_provider && <span className="source-provider">{s.source_provider}</span>}
            <span className="source-score">{Math.round(s.score * 100)}%</span>
          </div>
          <strong>{s.title || host(s.url)}</strong>
          <small>{host(s.url)}</small>
          {s.snippet && <p className="source-snippet">{s.snippet.slice(0, 120)}</p>}
        </a>
      ))}
      {!sources.length && <p className="empty-note">No sources discovered yet.</p>}
    </div>
  );
}

// ── Evidence ──────────────────────────────────────────────

function EvidenceView({ evidence }: { evidence: EvidenceSummary[] }) {
  const [filterDim, setFilterDim] = useState("all");
  const dims = ["all", ...Array.from(new Set(evidence.map((e) => e.dimension_label))).filter(Boolean)];
  const filtered = filterDim === "all" ? evidence : evidence.filter((e) => e.dimension_label === filterDim);

  return (
    <div className="artifact-scroll">
      {evidence.length > 0 && (
        <div className="filter-bar">
          {dims.map((d) => (
            <button key={d} className={filterDim === d ? "active" : ""} onClick={() => setFilterDim(d)}>{d}</button>
          ))}
        </div>
      )}
      {filtered.map((item) => (
        <article className="evidence-item" key={item.evidence_id}>
          <div className="evidence-item-top">
            <span className="dim-badge">{item.dimension_label}</span>
            {item.competitor && <span className="competitor-badge">{item.competitor}</span>}
            <ConfidenceBar value={item.confidence} />
          </div>
          <strong>{item.fact}</strong>
          {item.quote_preview && <blockquote className="evidence-quote">"{item.quote_preview}"</blockquote>}
          <a href={item.source_url} target="_blank" rel="noreferrer" className="evidence-source">
            <Globe2 size={12} />
            {item.source_title || host(item.source_url)}
            <ExternalLink size={11} />
          </a>
        </article>
      ))}
      {!evidence.length && <p className="empty-note">Evidence extraction has not produced items yet.</p>}
    </div>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "var(--green)" : pct >= 40 ? "var(--amber)" : "var(--red)";
  return (
    <div className="conf-bar" title={`Confidence: ${pct}%`}>
      <div style={{ width: `${pct}%`, background: color }} />
      <span style={{ color }}>{pct}%</span>
    </div>
  );
}

// ── Claims ────────────────────────────────────────────────

function ClaimsView({ claims }: { claims: Claim[] }) {
  const [filterRisk, setFilterRisk] = useState("all");
  const filtered = filterRisk === "all" ? claims : claims.filter((c) => c.risk_level === filterRisk);

  return (
    <div className="artifact-scroll">
      <div className="filter-bar">
        {(["all", "low", "medium", "high"] as const).map((r) => (
          <button key={r} className={filterRisk === r ? "active" : ""} onClick={() => setFilterRisk(r)}>
            {r === "all" ? "All" : r.charAt(0).toUpperCase() + r.slice(1) + " risk"}
            {r !== "all" && (
              <span className="filter-count">{claims.filter((c) => c.risk_level === r).length}</span>
            )}
          </button>
        ))}
      </div>
      {filtered.map((claim) => <ClaimCard key={claim.claim_id} claim={claim} />)}
      {!claims.length && <p className="empty-note">Claims will appear after evidence review.</p>}
    </div>
  );
}

function ClaimCard({ claim }: { claim: Claim }) {
  const [open, setOpen] = useState(false);
  return (
    <article className="claim-item">
      <div className="claim-item-top">
        <span className="dim-badge">{claim.dimension_label}</span>
        <RiskTag risk={claim.risk_level} />
        <ConfidenceBar value={claim.confidence} />
      </div>
      <strong>{claim.final_wording || claim.claim}</strong>
      <div className="claim-meta">
        <span className="claim-status">{claim.verification_status}</span>
        <span className="claim-evidence">{claim.supporting_evidence_ids.length} supporting evidence</span>
      </div>
      {claim.reasoning_summary && <p className="claim-reasoning">{claim.reasoning_summary}</p>}
      {claim.red_team_notes.length > 0 && (
        <div className="red-team-section">
          <button className="red-team-toggle" onClick={() => setOpen((p) => !p)}>
            <ShieldCheck size={13} />
            {claim.red_team_notes.length} red-team note{claim.red_team_notes.length > 1 ? "s" : ""}
            <ChevronDown size={12} style={{ transform: open ? "rotate(180deg)" : "none", transition: "0.2s" }} />
          </button>
          {open && claim.red_team_notes.map((note, i) => (
            <div key={i} className={`red-team-note ${note.severity}`}>
              <span className="rtn-type">{note.risk_type}</span>
              <p>{note.comment}</p>
              <small>{note.suggested_action}</small>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

// ── Report ────────────────────────────────────────────────

type ReportTab = "executive" | "full" | "methodology" | "assistant";

function ReportView({ detail }: { detail: RunDetail }) {
  const [tab, setTab] = useState<ReportTab>("executive");
  const tabsRef = useRef<HTMLDivElement>(null);
  const hasReport = !!detail.report_markdown?.trim();
  const content = {
    executive: detail.executive_summary_markdown || detail.report_markdown,
    full: detail.report_markdown,
    methodology: detail.methodology_markdown || "",
    assistant: "",
  }[tab];

  async function copyMarkdown() {
    if (tab === "assistant") return;
    try { await navigator.clipboard.writeText(content ?? ""); } catch { /* ignore */ }
  }

  function downloadMarkdown() {
    if (tab === "assistant") return;
    const blob = new Blob([content ?? ""], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${detail.request.target_product}_${tab}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  useEffect(() => {
    const activeButton = tabsRef.current?.querySelector<HTMLButtonElement>("button.active");
    activeButton?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
  }, [tab]);

  return (
    <div className="report-view">
      <div className="report-toolbar">
        <div className="report-tabs-wrap">
          <div className="report-tabs" ref={tabsRef}>
            {(["executive", "full", "methodology", "assistant"] as ReportTab[]).map((t) => (
              <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                {t === "executive" ? "Summary" : t === "full" ? "Full Report" : t === "methodology" ? "Methodology" : "AI Assistant"}
              </button>
            ))}
          </div>
        </div>
        <div className="report-actions" aria-hidden={tab === "assistant"}>
          <button className="report-action-btn" onClick={() => void copyMarkdown()} title="Copy markdown"><Copy size={14} /></button>
          <button className="report-action-btn" onClick={downloadMarkdown} title="Download .md"><Download size={14} /></button>
        </div>
      </div>
      <div className="report-content">
        {tab === "assistant" ? (
          <ChatPanel key={detail.status.run_id} runId={detail.status.run_id} disabled={!hasReport} />
        ) : content ? (
          <MarkdownContent content={content} />
        ) : (
          <div className="report-waiting">
            <FileText size={24} />
            <p>Report Composer is waiting for reviewed claims.</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// MarkdownContent
// ============================================================

type MdBlock =
  | { type: "h1" | "h2" | "h3" | "h4"; text: string }
  | { type: "hr" }
  | { type: "code"; lang: string; text: string }
  | { type: "blockquote"; text: string }
  | { type: "ul"; items: string[] }
  | { type: "ol"; items: string[] }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "p"; text: string };

function parseMarkdownBlocks(text: string): MdBlock[] {
  const lines = text.split("\n");
  const blocks: MdBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i]!;
    if (!line.trim()) { i++; continue; }

    const hm = line.match(/^(#{1,4}) +(.+)/);
    if (hm) {
      const level = Math.min(hm[1]!.length, 4) as 1 | 2 | 3 | 4;
      blocks.push({ type: `h${level}` as "h1" | "h2" | "h3" | "h4", text: hm[2]!.trim() });
      i++; continue;
    }

    if (/^[-*_]{3,}$/.test(line.trim())) { blocks.push({ type: "hr" }); i++; continue; }

    if (line.startsWith("```")) {
      const lang = line.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i]!.startsWith("```")) { codeLines.push(lines[i]!); i++; }
      i++;
      blocks.push({ type: "code", lang, text: codeLines.join("\n") });
      continue;
    }

    if (line.startsWith("> ")) { blocks.push({ type: "blockquote", text: line.slice(2) }); i++; continue; }

    if (line.includes("|") && i + 1 < lines.length && /^\|?[-:|  ]+\|/.test(lines[i + 1]!)) {
      const headers = line.split("|").map((h) => h.trim()).filter(Boolean);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i]!.includes("|")) {
        const cells = lines[i]!.split("|").map((c) => c.trim()).filter(Boolean);
        if (cells.length) rows.push(cells);
        i++;
      }
      blocks.push({ type: "table", headers, rows }); continue;
    }

    if (/^\[\d+\] /.test(line)) {
      while (i < lines.length && /^\[\d+\] /.test(lines[i]!)) {
        blocks.push({ type: "p", text: lines[i]! });
        i++;
      }
      continue;
    }

    if (/^[-*] /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*] /.test(lines[i]!)) { items.push(lines[i]!.replace(/^[-*] /, "")); i++; }
      blocks.push({ type: "ul", items }); continue;
    }

    if (/^\d+\. /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\. /.test(lines[i]!)) { items.push(lines[i]!.replace(/^\d+\. /, "")); i++; }
      blocks.push({ type: "ol", items }); continue;
    }

    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i]!.trim() !== "" &&
      !/^#{1,4} /.test(lines[i]!) &&
      !lines[i]!.startsWith("```") &&
      !lines[i]!.startsWith("> ") &&
      !/^[-*] /.test(lines[i]!) &&
      !/^\d+\. /.test(lines[i]!) &&
      !/^[-*_]{3,}$/.test(lines[i]!.trim())
    ) { paraLines.push(lines[i]!); i++; }

    if (paraLines.length) blocks.push({ type: "p", text: paraLines.join(" ") });
    else i++;
  }
  return blocks;
}

function applyInlineMarkdown(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(.+?)__/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function InlineText({ text }: { text: string }) {
  return <span dangerouslySetInnerHTML={{ __html: applyInlineMarkdown(text) }} />;
}

function MdBlock({ block }: { block: MdBlock }) {
  if (block.type === "hr") return <hr />;
  if (block.type === "code") return <pre className="md-code-block"><code>{block.text}</code></pre>;
  if (block.type === "table") return (
    <div className="md-table-wrap">
      <table>
        <thead><tr>{block.headers.map((h, j) => <th key={j}><InlineText text={h} /></th>)}</tr></thead>
        <tbody>{block.rows.map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci}><InlineText text={cell} /></td>)}</tr>)}</tbody>
      </table>
    </div>
  );
  if (block.type === "ul") return <ul>{block.items.map((it, i) => <li key={i}><InlineText text={it} /></li>)}</ul>;
  if (block.type === "ol") return <ol>{block.items.map((it, i) => <li key={i}><InlineText text={it} /></li>)}</ol>;
  if (block.type === "blockquote") return <blockquote><InlineText text={block.text} /></blockquote>;
  if (block.type === "h1") return <h1><InlineText text={block.text} /></h1>;
  if (block.type === "h2") return <h2><InlineText text={block.text} /></h2>;
  if (block.type === "h3") return <h3><InlineText text={block.text} /></h3>;
  if (block.type === "h4") return <h4><InlineText text={block.text} /></h4>;
  return <p><InlineText text={block.text} /></p>;
}

function MarkdownContent({ content }: { content: string }) {
  const blocks = useMemo(() => parseMarkdownBlocks(content || ""), [content]);
  return <div className="md-content">{blocks.map((block, i) => <MdBlock key={i} block={block} />)}</div>;
}

// ============================================================
// Small reusable components
// ============================================================

function MiniStat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="mini-stat">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function RunBadge({ status }: { status: RunStatusName }) {
  const icons: Record<RunStatusName, React.ReactNode> = {
    queued:    <CircleDashed size={13} />,
    running:   <Loader2 size={13} className="spin" />,
    completed: <CheckCircle2 size={13} />,
    failed:    <XCircle size={13} />,
    stopped:   <CircleStop size={13} />,
  };
  return (
    <span className={`run-badge ${status}`}>
      {icons[status]}
      {status}
    </span>
  );
}

function RiskTag({ risk }: { risk: Claim["risk_level"] }) {
  return <span className={`risk-tag ${risk}`}>{risk} risk</span>;
}

// ============================================================
// Utility functions
// ============================================================

function defaultDraft(): DraftState {
  return {
    projectName: "AI coding assistant landscape",
    target: "Trae",
    productDescription: "一个 AI 编程助手 / AI coding tool",
    competitors: "Cursor, GitHub Copilot, Windsurf",
    goal: "Analyze public evidence for Trae against Cursor, GitHub Copilot, and Windsurf. Produce positioning, pricing, enterprise readiness, user voice, opportunity points, and sales battlecards.",
    seedUrls: "",
    dimensions: ["positioning", "feature", "pricing", "user_voice", "enterprise", "strategy"],
    maxSources: 150,
    maxSourcesPerQuery: 3,
    maxSearchRounds: 3,
    showAdvanced: false,
  };
}

function runProgress(status: RunStatus | null): number {
  if (!status) return 0;
  if (status.status === "completed") return 100;
  if (status.status === "stopped") return Math.max(8, completedStages(status) * 20);
  if (status.status === "failed") return Math.max(8, completedStages(status) * 20);
  return Math.max(8, completedStages(status) * 20 + (status.status === "running" ? 8 : 0));
}

function completedStages(status: RunStatus): number {
  return stageOrder.filter((s) => status.node_status[s] === "completed").length;
}

function stageState(status: RunStatus, stage: string): NodeState | "" {
  const val = status.node_status[stage];
  if (val === "completed") return "done";
  if (val === "failed")    return "failed";
  if (normalizeStage(status.current_stage) === stage || val === "running") return "active";
  return "";
}

function normalizeStage(stage: string): string {
  if (stageOrder.includes(stage)) return stage;
  return stageOrder.find((s) => stage.includes(s)) ?? "ResearchPlanningAgent";
}

function agentLabel(stage: string): string {
  return {
    ResearchPlanningAgent: "Planning",
    SourceResearchAgent: "Source research",
    EvidenceStructuringAgent: "Evidence structuring",
    AnalysisAndReviewAgent: "Analysis review",
    ReportComposerAgent: "Report composer",
  }[stage] ?? stage;
}

function formatTraceMessage(event: TraceEvent): string {
  let message = event.message;
  message = message.replace(/第 (\d+)\/\d+ 轮/g, "第 $1 轮");
  message = message.replace(
    "两阶段内容搜索（Planning 批量铺底 + Search LLM ReAct 补洞）",
    "两阶段内容搜索（批量Query快速获取信息 + Search LLM针对性补充）",
  );
  message = message.replace(
    "两阶段内容搜索（先用批量的Query搜索，再结合Search LLM补充）",
    "两阶段内容搜索（批量Query快速获取信息 + Search LLM针对性补充）",
  );
  message = message.replace(
    /第 1 轮：执行 Planning 生成的 (\d+) 条查询（不调用 Search LLM）/,
    "执行 $1 条 Query 快速获取相关信息",
  );
  message = message.replace(
    /第 1 轮：执行用于快速搜索信息生成的 (\d+) 条Query/,
    "执行 $1 条 Query 快速获取相关信息",
  );
  message = message.replace(
    /第 1 轮：使用规划阶段生成的 (\d+) 个搜索任务/,
    "执行 $1 条 Query 快速获取相关信息",
  );
  message = message.replace("从真实正文中抽取结构化 Evidence", "从已采集正文中抽取结构化 Evidence");
  message = message.replace(
    "生成结论、反方审查、评估信息缺口",
    "生成 Claim、执行 Red Team 审查并评估证据缺口",
  );
  message = message.replace(
    "缺口评估：信息充足",
    "证据覆盖已满足当前质量门禁，研究链路将在第 1 轮后收敛，无需启动第 2 轮",
  );
  message = message.replace(
    "缺口评估：需要补充搜索",
    "证据缺口评估完成：需要启动下一轮补充研究",
  );
  return message;
}

function dimensionLabel(dim: string): string {
  const map: Record<string, string> = {
    positioning: "定位", feature: "功能", pricing: "定价",
    user_voice: "口碑", enterprise: "企业", strategy: "战略",
  };
  return map[dim] ?? dim;
}

function normalizeList(value: string): string[] {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

function normalizeLines(value: string): string[] {
  return value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function formatShortDate(value: string): string {
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function host(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

function truncateUrl(url: string): string {
  try {
    const u = new URL(url);
    const path = u.pathname.length > 28 ? u.pathname.slice(0, 28) + "…" : u.pathname;
    return u.hostname.replace(/^www\./, "") + path;
  } catch { return url.slice(0, 50); }
}
