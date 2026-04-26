/* Command Center Dashboard Plugin v0.2 */
(function () {
  "use strict";

  if (!window.__HERMES_PLUGIN_SDK__ || !window.__HERMES_PLUGINS__) {
    console.error("Hermes plugin SDK unavailable for Command Center");
    return;
  }

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var e = React.createElement;
  var Card = SDK.components.Card;
  var CardHeader = SDK.components.CardHeader;
  var CardTitle = SDK.components.CardTitle;
  var CardContent = SDK.components.CardContent;
  var Badge = SDK.components.Badge;
  var Button = SDK.components.Button;

  function cls() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
  }

  function fmt(value) {
    if (value === null || value === undefined || value === "") return "—";
    return String(value);
  }

  function daysLabel(days) {
    if (days === null || days === undefined) return "unknown";
    if (days === 0) return "today";
    if (days === 1) return "1 day ago";
    return days + " days ago";
  }

  function severityClass(sev) {
    if (sev === "critical") return "border-red-500/50 bg-red-500/10";
    if (sev === "warning") return "border-yellow-500/50 bg-yellow-500/10";
    return "border-blue-500/30 bg-blue-500/5";
  }

  function itemClass(item) {
    if (item.is_stale) return "border-red-500/40 bg-red-500/8";
    if (item.status_group === "active") return "border-emerald-500/30 bg-emerald-500/5";
    if (item.status_group === "paused") return "border-slate-500/30 bg-slate-500/5";
    if (item.type === "wiki_health") return severityClass(item.status);
    return "border-border/70 bg-card/70";
  }

  function copy(text, setCopied) {
    if (!navigator.clipboard || !navigator.clipboard.writeText) return;
    navigator.clipboard.writeText(text || "").then(function () {
      if (setCopied) {
        setCopied("Copied");
        setTimeout(function () { setCopied(""); }, 1200);
      }
    }).catch(function () {
      if (setCopied) setCopied("Copy failed");
    });
  }

  function Stat(props) {
    return e(Card, { className: "border-border/70 bg-card/70" },
      e(CardContent, { className: "p-4" },
        e("div", { className: "text-2xl font-semibold" }, String(props.value || 0)),
        e("div", { className: "mt-1 text-xs uppercase tracking-wide text-muted-foreground" }, props.label)
      )
    );
  }

  function OpTile(props) {
    return e(Card, { className: cls("border", props.tone || "border-border/70 bg-card/70") },
      e(CardContent, { className: "p-4" },
        e("div", { className: "text-xs uppercase tracking-[0.16em] text-muted-foreground" }, props.label),
        e("div", { className: "mt-2 text-2xl font-semibold" }, props.value),
        props.detail ? e("div", { className: "mt-1 text-xs text-muted-foreground" }, props.detail) : null
      )
    );
  }

  function MissionHero(props) {
    var data = props.data;
    var start = props.start;
    var setCopied = props.setCopied;
    var loading = props.loading;
    var load = props.load;
    var copied = props.copied;
    var counts = data.counts || {};
    var status = counts.critical > 0 ? "Critical" : (counts.warnings > 0 || counts.stale > 0 ? "Needs Attention" : "Operational");
    var tone = status === "Critical" ? "border-red-500/50 bg-red-500/10" : status === "Needs Attention" ? "border-yellow-500/40 bg-yellow-500/10" : "border-emerald-500/30 bg-emerald-500/8";
    var summary = counts.sources + " sources · " + counts.active + " active · " + counts.stale + " stale · " + counts.warnings + " warnings";
    return e(Card, { className: cls("border shadow-sm", tone) },
      e(CardContent, { className: "p-5" },
        e("div", { className: "flex flex-wrap items-start justify-between gap-4" },
          e("div", null,
            e("div", { className: "text-xs font-semibold uppercase tracking-[0.22em] text-muted-foreground" }, "Mission Control"),
            e("div", { className: "mt-2 flex flex-wrap items-center gap-2" },
              e("h1", { className: "text-3xl font-semibold tracking-tight" }, "Command Center"),
              e(Badge, { variant: status === "Critical" ? "destructive" : "secondary" }, status)
            ),
            e("p", { className: "mt-2 text-sm text-muted-foreground" }, summary)
          ),
          e("div", { className: "flex items-center gap-2" },
            copied ? e(Badge, { variant: copied === "Copied" ? "default" : "destructive" }, copied) : null,
            e(Button, { variant: "outline", onClick: load }, loading ? "Refreshing…" : "Refresh"),
            e(Button, { variant: "secondary", onClick: function () { copy("Review my Command Center source diagnostics, work health checks, and restart scripts. Recommend the one best next action.", setCopied); } }, "Copy review prompt")
          )
        ),
        e("div", { className: "mt-5 rounded-lg border border-border/70 bg-background/50 p-4" },
          e("div", { className: "text-xs font-semibold uppercase tracking-[0.18em] text-primary" }, "Primary Objective"),
          start ? [
            e("div", { key: "title", className: "mt-2 text-xl font-semibold" }, start.title),
            e("div", { key: "do", className: "mt-2 text-sm leading-6" }, start.do_first),
            e("div", { key: "success", className: "mt-2 text-xs text-muted-foreground" }, "Success: ", start.success_condition)
          ] : e("p", { className: "mt-2 text-sm text-muted-foreground" }, "No primary objective available yet."),
          start ? e(Button, { className: "mt-3", onClick: function () { copy(start.prompt, setCopied); } }, "Copy focus prompt") : null
        )
      )
    );
  }

  function SetupCard(props) {
    var data = props.data;
    var setCopied = props.setCopied;
    var ops = data.ops || {};
    var cfg = data.config || {};
    return e(Card, { className: cls("border", cfg.setup_needed ? "border-yellow-500/40 bg-yellow-500/10" : "border-emerald-500/30 bg-emerald-500/5") },
      e(CardContent, { className: "p-4" },
        e("div", { className: "flex flex-wrap items-start justify-between gap-3" },
          e("div", null,
            e("div", { className: "font-semibold" }, cfg.setup_needed ? "Setup needed: tune this Command Center" : "Setup configured"),
            e("p", { className: "mt-1 text-sm text-muted-foreground" }, cfg.setup_needed ? "Run one local setup pass to choose real roots and conventions." : "Using " + (cfg.config_source || "explicit config") + "."),
            e("p", { className: "mt-1 text-xs text-muted-foreground" }, "Target config: ", cfg.config_path || "~/.hermes/command-center.json")
          ),
          e(Button, { variant: cfg.setup_needed ? "default" : "outline", onClick: function () { copy(ops.setup_prompt || "", setCopied); } }, "Copy setup prompt")
        )
      )
    );
  }

  function AttentionQueue(props) {
    var items = props.items || [];
    var checks = props.checks || [];
    var inbox = props.inbox || [];
    var setCopied = props.setCopied;
    var queue = [];
    checks.filter(function (c) { return c.severity === "critical" || c.severity === "warning"; }).slice(0, 3).forEach(function (c) {
      queue.push({ kind: "Alert", title: c.title, detail: c.message, path: c.path, severity: c.severity });
    });
    items.filter(function (i) { return i.is_stale; }).slice(0, 3).forEach(function (i) {
      queue.push({ kind: "Stale", title: i.title, detail: (i.recommended_action && i.recommended_action.text) || i.summary || "Needs review", path: i.path || i.relative_path, severity: "warning" });
    });
    inbox.slice(0, 2).forEach(function (i) {
      queue.push({ kind: "Inbox", title: i.title, detail: i.snippet || "Needs triage", path: i.path || i.relative_path, severity: "info" });
    });
    queue = queue.slice(0, 6);
    return e(Card, null,
      e(CardHeader, { className: "pb-2" }, e(CardTitle, null, "Attention Queue")),
      e(CardContent, { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3" },
        queue.length ? queue.map(function (q, idx) {
          return e("div", { key: idx, className: cls("rounded-lg border p-3", severityClass(q.severity)) },
            e("div", { className: "flex items-start justify-between gap-2" },
              e("div", null,
                e("div", { className: "text-xs font-medium uppercase tracking-wide text-muted-foreground" }, q.kind),
                e("div", { className: "mt-1 font-semibold" }, q.title),
                e("div", { className: "mt-1 line-clamp-2 text-sm leading-6 text-muted-foreground" }, q.detail)
              ),
              q.path ? e(Button, { size: "sm", variant: "ghost", onClick: function () { copy(q.path, setCopied); } }, "Path") : null
            )
          );
        }) : e("p", { className: "text-sm text-muted-foreground" }, "No immediate attention items.")
      )
    );
  }

  function SourceCard(props) {
    var s = props.source;
    var diagCount = (s.diagnostics || []).length;
    return e(Card, { className: cls("border", s.status === "ok" ? "border-emerald-500/30" : s.status === "disabled" ? "border-slate-500/30" : "border-yellow-500/40") },
      e(CardContent, { className: "p-4" },
        e("div", { className: "flex items-start justify-between gap-3" },
          e("div", null,
            e("div", { className: "font-semibold" }, s.label),
            e("div", { className: "text-xs text-muted-foreground" }, s.id, " · ", s.type)
          ),
          e(Badge, { variant: s.status === "ok" ? "default" : "secondary" }, s.status)
        ),
        e("div", { className: "mt-3 grid grid-cols-3 gap-2 text-xs text-muted-foreground" },
          e("div", null, e("b", { className: "text-foreground" }, s.work_items || 0), " items"),
          e("div", null, e("b", { className: "text-foreground" }, s.health_checks || 0), " checks"),
          e("div", null, e("b", { className: "text-foreground" }, diagCount), " diag")
        ),
        diagCount ? e("div", { className: "mt-3 space-y-1" }, (s.diagnostics || []).slice(0, 3).map(function (d) {
          return e("div", { key: d.id, className: "text-xs text-muted-foreground" }, "• ", d.title);
        })) : null
      )
    );
  }

  function RestartCard(props) {
    var script = props.script;
    var setCopied = props.setCopied;
    return e(Card, { className: "border-primary/40 bg-primary/8" },
      e(CardHeader, { className: "pb-2" },
        e("div", { className: "flex items-start justify-between gap-3" },
          e("div", null,
            e("div", { className: "text-xs font-semibold uppercase tracking-[0.18em] text-primary" }, props.primary ? "Start Here" : "Restart Script"),
            e(CardTitle, { className: "mt-1 text-xl" }, script ? script.title : "No restart candidate")
          ),
          script ? e(Badge, { variant: "secondary" }, script.reason) : null
        )
      ),
      e(CardContent, { className: "space-y-3" },
        script ? [
          e("div", { key: "open", className: "rounded-lg border border-border/70 bg-background/50 p-3" },
            e("div", { className: "text-xs font-medium uppercase tracking-wide text-muted-foreground" }, "Open"),
            e("div", { className: "mt-1 text-sm" }, script.open)
          ),
          e("div", { key: "do", className: "rounded-lg border border-border/70 bg-background/50 p-3" },
            e("div", { className: "text-xs font-medium uppercase tracking-wide text-muted-foreground" }, "Do first"),
            e("div", { className: "mt-1 text-sm leading-6" }, script.do_first)
          ),
          e("div", { key: "success", className: "text-sm text-muted-foreground" }, e("b", { className: "text-foreground" }, "Success: "), script.success_condition),
          e("div", { key: "stop", className: "text-sm text-muted-foreground" }, e("b", { className: "text-foreground" }, "Stop: "), script.stop_boundary),
          e(Button, { key: "copy", onClick: function () { copy(script.prompt, setCopied); } }, "Copy focus prompt")
        ] : e("p", { className: "text-muted-foreground" }, "No active item or health check has a concrete restart action yet.")
      )
    );
  }

  function WorkItemCard(props) {
    var item = props.item;
    var setCopied = props.setCopied;
    var action = item.recommended_action || (item.next_actions && item.next_actions[0]);
    var prompt = action ? "Help me make progress on " + item.title + ": " + action.text : "Review this item and suggest one next action: " + item.title;
    return e(Card, { className: cls("border", itemClass(item)) },
      e(CardContent, { className: "p-4" },
        e("div", { className: "flex items-start justify-between gap-3" },
          e("div", { className: "min-w-0" },
            e("div", { className: "truncate font-semibold" }, item.title),
            e("div", { className: "mt-1 truncate text-xs text-muted-foreground" }, item.source_label || item.source, " · ", item.relative_path || item.path || item.type)
          ),
          e("div", { className: "flex shrink-0 gap-1" },
            e(Badge, { variant: item.status_group === "active" ? "default" : "secondary" }, item.status || item.status_group),
            item.is_stale ? e(Badge, { variant: "destructive" }, "stale") : null
          )
        ),
        item.summary ? e("p", { className: "mt-3 line-clamp-3 text-sm leading-6 text-muted-foreground" }, item.summary) : null,
        action ? e("div", { className: "mt-4 rounded-lg border border-border/70 bg-background/50 p-3" },
          e("div", { className: "mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground" }, "Recommended action"),
          e("div", { className: "text-sm leading-6" }, action.text),
          e("div", { className: "mt-2 text-xs text-muted-foreground" }, action.estimate_minutes ? action.estimate_minutes + " min" : "no estimate")
        ) : null,
        e("div", { className: "mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground" },
          e("span", null, "Updated: ", daysLabel(item.days_since_activity)),
          e("span", null, "Type: ", item.type)
        ),
        e("div", { className: "mt-3 flex flex-wrap gap-2" },
          e(Button, { size: "sm", variant: "outline", onClick: function () { copy(prompt, setCopied); } }, "Copy prompt"),
          e(Button, { size: "sm", variant: "ghost", onClick: function () { copy(item.path || item.relative_path || "", setCopied); } }, "Copy path")
        )
      )
    );
  }

  function HealthCheckRow(props) {
    var c = props.check;
    var setCopied = props.setCopied;
    return e("div", { className: cls("rounded-lg border p-3", severityClass(c.severity)) },
      e("div", { className: "flex items-start justify-between gap-2" },
        e("div", null,
          e("div", { className: "font-medium" }, c.title),
          e("div", { className: "mt-1 text-sm leading-6 text-muted-foreground" }, c.message),
          e("div", { className: "mt-1 text-xs text-muted-foreground" }, c.source, c.path ? " · " + c.path : "")
        ),
        e(Badge, { variant: c.severity === "critical" ? "destructive" : "secondary" }, c.severity)
      ),
      c.path ? e(Button, { className: "mt-2", size: "sm", variant: "ghost", onClick: function () { copy(c.path, setCopied); } }, "Copy path") : null
    );
  }

  function InboxCard(props) {
    var item = props.item;
    var setCopied = props.setCopied;
    return e("div", { className: "rounded-lg border border-border/70 bg-background/40 p-3" },
      e("div", { className: "flex items-start justify-between gap-2" },
        e("div", { className: "font-medium" }, item.title),
        e(Button, { size: "sm", variant: "ghost", onClick: function () { copy(item.path || item.relative_path || "", setCopied); } }, "Path")
      ),
      e("p", { className: "mt-2 line-clamp-3 text-sm leading-6 text-muted-foreground" }, item.snippet || "No preview."),
      e("div", { className: "mt-2 text-xs text-muted-foreground" }, item.relative_path || item.path)
    );
  }

  function CommandCenter() {
    var dataState = useState(null);
    var data = dataState[0];
    var setData = dataState[1];
    var loadingState = useState(true);
    var loading = loadingState[0];
    var setLoading = loadingState[1];
    var errorState = useState(null);
    var error = errorState[0];
    var setError = errorState[1];
    var filterState = useState("active");
    var filter = filterState[0];
    var setFilter = filterState[1];
    var copiedState = useState("");
    var copied = copiedState[0];
    var setCopied = copiedState[1];
    var queryState = useState("");
    var query = queryState[0];
    var setQuery = queryState[1];

    function load() {
      setLoading(true);
      setError(null);
      fetch("/api/plugins/command-center/data")
        .then(function (r) {
          if (!r.ok) throw new Error("Command Center API returned " + r.status);
          return r.json();
        })
        .then(setData)
        .catch(function (err) { setError(String(err && err.message ? err.message : err)); })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () { load(); }, []);

    if (loading && !data) return e("div", { className: "p-6 text-muted-foreground" }, "Loading Command Center…");
    if (error && !data) return e("div", { className: "p-6 text-red-500" }, error);

    var items = data.work_items || data.projects || [];
    var checks = data.health_checks || [];
    var sources = data.sources || [];
    var inbox = data.inbox_items || data.captures || [];
    var scripts = data.restart_scripts || [];
    var start = data.start_here || data.start_task;
    var q = query.toLowerCase().trim();
    var visible = items.filter(function (item) {
      var matchesFilter = filter === "all" ||
        (filter === "stale" && item.is_stale) ||
        (filter === "health" && item.type === "wiki_health") ||
        (filter === "code" && item.type === "git_repo") ||
        (filter === "sessions" && item.type === "hermes_session") ||
        item.status_group === filter || item.status === filter;
      var matchesQuery = !q || [item.title, item.path, item.relative_path, item.source, item.summary].join(" ").toLowerCase().indexOf(q) >= 0;
      return matchesFilter && matchesQuery;
    });

    return e("div", { className: "space-y-5 p-5" },
      e(MissionHero, { data: data, start: start, setCopied: setCopied, loading: loading, load: load, copied: copied }),

      e(SetupCard, { data: data, setCopied: setCopied }),

      e("div", { className: "grid gap-3 md:grid-cols-4 lg:grid-cols-8" },
        e(OpTile, { label: "Sources", value: data.counts.sources, detail: "configured" }),
        e(OpTile, { label: "Items", value: data.counts.work_items, detail: "tracked" }),
        e(OpTile, { label: "Active", value: data.counts.active, detail: "in motion", tone: "border-emerald-500/30 bg-emerald-500/5" }),
        e(OpTile, { label: "Stale", value: data.counts.stale, detail: "needs restart", tone: data.counts.stale ? "border-yellow-500/40 bg-yellow-500/10" : "border-border/70 bg-card/70" }),
        e(OpTile, { label: "Inbox", value: data.counts.inbox, detail: "untriaged" }),
        e(OpTile, { label: "Checks", value: data.counts.health_checks, detail: "detected" }),
        e(OpTile, { label: "Warnings", value: data.counts.warnings, detail: "attention", tone: data.counts.warnings ? "border-yellow-500/40 bg-yellow-500/10" : "border-border/70 bg-card/70" }),
        e(OpTile, { label: "Restarts", value: data.counts.restart_scripts, detail: "ready prompts" })
      ),

      e(AttentionQueue, { items: items, checks: checks, inbox: inbox, setCopied: setCopied }),

      e("div", { className: "grid gap-5 xl:grid-cols-[1fr_380px]" },
        e("div", { className: "space-y-5" },
          e(Card, null,
            e(CardHeader, { className: "pb-2" }, e(CardTitle, null, "Source Diagnostics")),
            e(CardContent, { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3" },
              sources.length ? sources.map(function (s) { return e(SourceCard, { key: s.id, source: s }); }) : e("p", { className: "text-sm text-muted-foreground" }, "No sources configured.")
            )
          ),

          e("div", { className: "space-y-3" },
            e("div", { className: "flex flex-wrap items-center justify-between gap-2" },
              e("h2", { className: "text-lg font-semibold" }, "Mission Feed"),
              e("div", { className: "flex flex-wrap gap-2" },
                ["active", "stale", "code", "sessions", "health", "paused", "waiting", "done", "all"].map(function (name) {
                  return e(Button, { key: name, size: "sm", variant: filter === name ? "default" : "outline", onClick: function () { setFilter(name); } }, name);
                })
              )
            ),
            e("input", {
              className: "w-full rounded-md border border-border bg-background px-3 py-2 text-sm",
              placeholder: "Filter by title, source, path…",
              value: query,
              onChange: function (ev) { setQuery(ev.target.value); }
            }),
            e("div", { className: "grid gap-3 lg:grid-cols-2" },
              visible.slice(0, 80).map(function (item) { return e(WorkItemCard, { key: item.id, item: item, setCopied: setCopied }); })
            )
          )
        ),

        e("div", { className: "space-y-5" },
          e(Card, null,
            e(CardHeader, { className: "pb-2" }, e(CardTitle, null, "Health Checks")),
            e(CardContent, { className: "space-y-3" },
              checks.length ? checks.slice(0, 20).map(function (c) { return e(HealthCheckRow, { key: c.id, check: c, setCopied: setCopied }); }) : e("p", { className: "text-sm text-muted-foreground" }, "No health checks found.")
            )
          ),
          e(Card, null,
            e(CardHeader, { className: "pb-2" }, e(CardTitle, null, "Restart Scripts")),
            e(CardContent, { className: "space-y-3" },
              scripts.length ? scripts.slice(0, 6).map(function (s) { return e(RestartCard, { key: s.id, script: s, primary: false, setCopied: setCopied }); }) : e("p", { className: "text-sm text-muted-foreground" }, "No restart scripts available.")
            )
          ),
          e(Card, null,
            e(CardHeader, { className: "pb-2" }, e(CardTitle, null, "Inbox")),
            e(CardContent, { className: "space-y-3" },
              inbox.length ? inbox.map(function (item) { return e(InboxCard, { key: item.id, item: item, setCopied: setCopied }); }) : e("p", { className: "text-sm text-muted-foreground" }, "No inbox items found.")
            )
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("command-center", CommandCenter);
})();
