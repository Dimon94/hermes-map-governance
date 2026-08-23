(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry) {
    return;
  }

  const { React, fetchJSON } = SDK;
  const { useCallback, useEffect, useState } = SDK.hooks;
  const {
    Badge,
    Button,
    Card,
    CardContent,
    CardHeader,
    CardTitle,
  } = SDK.components;

  function locationProfile() {
    const search = window.location && window.location.search;
    if (!search) {
      return null;
    }
    return new URLSearchParams(search).get("profile");
  }

  function requestProfile() {
    const explicitProfile = locationProfile();
    if (explicitProfile) {
      return Promise.resolve(explicitProfile);
    }
    if (SDK.host && SDK.host.state && SDK.host.state.profile) {
      return Promise.resolve(SDK.host.state.profile.get());
    }
    if (SDK.api && typeof SDK.api.getActiveProfile === "function") {
      return SDK.api.getActiveProfile().then(function (result) {
        return result.current || result.active || "default";
      });
    }
    return Promise.resolve("default");
  }

  function renderDecision(decision, key, className) {
    return React.createElement(
      "article",
      { key: key, className: className },
      React.createElement(
        "p",
        { className: "font-medium text-foreground" },
        decision.type + " · " + decision.decision_id,
      ),
      React.createElement("p", null, decision.rationale),
      React.createElement(
        "time",
        { dateTime: decision.timestamp },
        decision.timestamp,
      ),
    );
  }

  function readableBadge(value) {
    return String(value || "").replaceAll("_", " ");
  }

  function renderPMReport(report, key, className) {
    return React.createElement(
      "article",
      {
        key: key,
        className: className,
        "aria-label": "PM report " + report.record_id,
      },
      React.createElement(
        "p",
        { className: "font-medium text-foreground" },
        report.type + " · " + report.record_id,
      ),
      React.createElement("p", null, report.summary),
      report.blocking !== undefined
        ? React.createElement("p", null, "Whole Map blocked: " + (report.blocking ? "yes" : "no"))
        : null,
      report.continuation_requirement
        ? React.createElement(
            "p",
            null,
            "Needed to continue: " + report.continuation_requirement,
          )
        : null,
      report.failure_code
        ? React.createElement("p", null, "Failure code: " + report.failure_code)
        : null,
      report.type === "acceptance" && (report.evidence || []).length
        ? React.createElement(
            "ul",
            { className: "list-disc space-y-1 pl-5", "aria-label": "Executive evidence" },
            report.evidence.map(function (item, index) {
              return React.createElement("li", { key: index }, item);
            }),
          )
        : null,
      React.createElement(
        "time",
        { dateTime: report.timestamp },
        report.timestamp,
      ),
    );
  }

  function safeExternalUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "https:" || parsed.protocol === "http:"
        ? parsed.href
        : null;
    } catch (_error) {
      return null;
    }
  }

  function MapsPage() {
    const [state, setState] = useState({ status: "loading" });
    const [transitionState, setTransitionState] = useState({ status: "idle" });
    const [sessionState, setSessionState] = useState({ status: "idle" });
    const [detailState, setDetailState] = useState({ status: "idle" });
    const [approvalState, setApprovalState] = useState({ status: "idle" });

    const load = useCallback(function () {
      setState({ status: "loading" });
      requestProfile().then(function (profile) {
        const encodedProfile = encodeURIComponent(profile);
        return Promise.all([
          fetchJSON("/api/plugins/map-governance/board?profile=" + encodedProfile),
          fetchJSON("/api/plugins/map-governance/health?profile=" + encodedProfile),
        ]);
      }).then(
        function (results) {
          setState({ status: "ready", board: results[0], health: results[1] });
        },
        function (error) {
          setState({
            status: "error",
            message: error && error.message ? error.message : "Unable to load Maps",
          });
        },
      );
    }, []);

    const refresh = useCallback(function () {
      setState({ status: "loading" });
      requestProfile().then(function (profile) {
        const encodedProfile = encodeURIComponent(profile);
        return fetchJSON(
          "/api/plugins/map-governance/refresh?profile=" + encodedProfile,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          },
        );
      }).then(
        load,
        function (error) {
          setState({
            status: "error",
            message: error && error.message ? error.message : "Unable to refresh Maps",
          });
        },
      );
    }, [load]);

    const transitionMap = useCallback(function (mapId, expectedStage, requestedStage) {
      let approval = null;
      if (requestedStage === "authorized") {
        const detail = detailState.mapId === mapId && detailState.status === "ready"
          ? detailState.detail
          : null;
        approval = detail && (detail.approvals.items || []).find(function (item) {
          return item.status === "approved"
            && item.proposed_action === "transition_map"
            && item.requested_scope.map_id === mapId
            && item.decision_payload.expected_stage === expectedStage
            && item.decision_payload.requested_stage === requestedStage;
        });
        if (!approval) {
          setTransitionState({
            status: "error",
            mapId: mapId,
            message: "Open Map detail and obtain a matching chairman approval first.",
          });
          return;
        }
      }
      if ((requestedStage === "authorized" || requestedStage === "parked")
          && !window.confirm(
            "Confirm major Map action: move from " + expectedStage
              + " to " + requestedStage + "?",
          )) {
        return;
      }
      setTransitionState({ status: "pending", mapId: mapId });
      requestProfile().then(function (profile) {
        const encodedProfile = encodeURIComponent(profile);
        return fetchJSON(
          "/api/plugins/map-governance/transitions?profile=" + encodedProfile,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign({
              map_id: mapId,
              expected_stage: expectedStage,
              requested_stage: requestedStage,
            }, approval ? {
              approval_request_id: approval.request_id,
              // One approval can authorize exactly one protected mutation, so its
              // stable request identity is also the stable retry identity.
              mutation_id: approval.request_id,
            } : {})),
          },
        );
      }).then(
        function () {
          setTransitionState({ status: "idle" });
          load();
        },
        function (error) {
          setTransitionState({
            status: "error",
            mapId: mapId,
            message: error && error.message
              ? error.message
              : "GitHub did not commit the requested transition",
          });
        },
      );
    }, [detailState, load]);

    const openCEOSession = useCallback(function (mapId) {
      setSessionState({ status: "pending", mapId: mapId });
      let requestedProfile = "default";
      requestProfile().then(function (profile) {
        requestedProfile = profile;
        const encodedProfile = encodeURIComponent(profile);
        return fetchJSON(
          "/api/plugins/map-governance/maps/" + encodeURIComponent(mapId)
            + "/session?profile=" + encodedProfile,
          { method: "POST" },
        );
      }).then(function (result) {
        if (!SDK.host || typeof SDK.host.openSession !== "function") {
          throw new Error("This Hermes Desktop version cannot open stored sessions");
        }
        return SDK.host.openSession(result.ceo_session.live_session_id, {
          profile: requestedProfile,
          intent: "main",
          keepAllProfilesScope: false,
          awaitHydration: true,
          expectHistory: true,
          retryHydrationTimeoutOnce: true,
        });
      }).then(
        function () {
          setSessionState({ status: "idle" });
          load();
        },
        function (error) {
          setSessionState({
            status: "error",
            mapId: mapId,
            message: error && error.message
              ? error.message
              : "Unable to open the canonical CEO session",
          });
          load();
        },
      );
    }, [load]);

    const loadMapDetail = useCallback(function (mapId) {
      setDetailState({ status: "loading", mapId: mapId });
      requestProfile().then(function (profile) {
        return fetchJSON(
          "/api/plugins/map-governance/maps/" + encodeURIComponent(mapId)
            + "?profile=" + encodeURIComponent(profile),
        );
      }).then(
        function (detail) {
          setDetailState({ status: "ready", mapId: mapId, detail: detail });
        },
        function (error) {
          setDetailState({
            status: "error",
            mapId: mapId,
            message: error && error.message
              ? error.message
              : "Unable to load Map detail",
          });
        },
      );
    }, []);

    const decideApproval = useCallback(function (mapId, requestId, decision) {
      const label = decision === "revision" ? "request revision" : decision;
      if (!window.confirm(
        "Confirm chairman decision: " + label + " approval " + requestId + "?",
      )) {
        return;
      }
      const note = window.prompt("Record the chairman rationale for this decision:");
      if (!note || !note.trim()) {
        setApprovalState({
          status: "error",
          mapId: mapId,
          message: "A chairman decision note is required.",
        });
        return;
      }
      setApprovalState({ status: "pending", mapId: mapId, requestId: requestId });
      requestProfile().then(function (profile) {
        return fetchJSON(
          "/api/plugins/map-governance/maps/" + encodeURIComponent(mapId)
            + "/approvals/" + encodeURIComponent(requestId)
            + "/decision?profile=" + encodeURIComponent(profile),
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decision: decision, note: note.trim() }),
          },
        );
      }).then(
        function () {
          setApprovalState({ status: "idle" });
          loadMapDetail(mapId);
          load();
        },
        function (error) {
          setApprovalState({
            status: "error",
            mapId: mapId,
            message: error && error.message
              ? error.message
              : "The approval decision was not confirmed by GitHub.",
          });
        },
      );
    }, [load, loadMapDetail]);

    useEffect(function () {
      load();
    }, [load]);

    if (state.status === "loading") {
      return React.createElement(
        "div",
        { className: "p-6 text-sm text-muted-foreground", role: "status" },
        "Loading Maps…",
      );
    }

    if (state.status === "error") {
      return React.createElement(
        Card,
        { className: "mx-auto mt-12 max-w-xl" },
        React.createElement(
          CardHeader,
          null,
          React.createElement(CardTitle, null, "Maps are unavailable"),
        ),
        React.createElement(
          CardContent,
          { className: "space-y-4" },
          React.createElement(
            "p",
            { className: "text-sm text-muted-foreground", role: "alert" },
            state.message,
          ),
          React.createElement(Button, { type: "button", onClick: load }, "Retry"),
        ),
      );
    }

    if (state.board.maps.length === 0) {
      return React.createElement(
        "section",
        { className: "mx-auto mt-12 max-w-2xl px-4", "aria-labelledby": "maps-empty-title" },
        React.createElement(
          Card,
          null,
          React.createElement(
            CardHeader,
            { className: "space-y-3" },
            React.createElement(
              "div",
              { className: "flex items-center justify-between gap-3" },
              React.createElement(CardTitle, { id: "maps-empty-title" }, state.board.empty_state.title),
              React.createElement(
                Badge,
                { variant: "outline" },
                state.health.status === "ready" ? "Plugin ready" : "Setup required",
              ),
            ),
          ),
          React.createElement(
            CardContent,
            { className: "space-y-4" },
            React.createElement(
              "p",
              { className: "text-sm text-muted-foreground" },
              state.board.empty_state.description,
            ),
            React.createElement(
              Button,
              { type: "button", onClick: refresh },
              "Refresh from GitHub",
            ),
          ),
        ),
      );
    }

    return React.createElement(
      "main",
      { className: "space-y-8 p-6", "aria-labelledby": "maps-title" },
      React.createElement(
        "header",
        { className: "flex items-center justify-between gap-4" },
        React.createElement("h1", { id: "maps-title", className: "text-xl font-semibold" }, "Maps"),
        React.createElement(Button, { type: "button", onClick: refresh }, "Refresh"),
      ),
      state.board.projects.map(function (project) {
        const headingId = "project-" + project.id;
        return React.createElement(
          "section",
          { key: project.id, className: "space-y-3", "aria-labelledby": headingId },
          React.createElement(
            "div",
            { className: "flex flex-wrap items-baseline justify-between gap-2" },
            React.createElement(
              "h2",
              { id: headingId, className: "text-base font-semibold" },
              project.title,
            ),
            React.createElement(
              "a",
              {
                href: project.tracker.url,
                target: "_blank",
                rel: "noreferrer",
                className: "text-xs text-muted-foreground underline-offset-4 hover:underline",
              },
              project.tracker.owner + " project #" + project.tracker.number,
            ),
          ),
          React.createElement(
            "div",
            { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3" },
            project.maps.map(function (card) {
              return React.createElement(
                Card,
                { key: card.id },
                React.createElement(
                  CardHeader,
                  { className: "space-y-2" },
                  React.createElement(
                    "div",
                    { className: "flex items-start justify-between gap-3" },
                    React.createElement(
                      CardTitle,
                      { className: "text-base" },
                      React.createElement(
                        "a",
                        {
                          href: card.tracker.url,
                          target: "_blank",
                          rel: "noreferrer",
                          className: "underline-offset-4 hover:underline",
                        },
                        card.title,
                      ),
                    ),
                    React.createElement(Badge, { variant: "outline" }, card.stage),
                  ),
                  React.createElement(
                    "p",
                    { className: "text-xs text-muted-foreground" },
                    card.tracker.identity,
                  ),
                ),
                React.createElement(
                  CardContent,
                  { className: "space-y-3 text-xs text-muted-foreground" },
                  React.createElement(
                    "section",
                    { className: "space-y-1", "aria-label": "Confirmed CEO decisions" },
                    React.createElement(
                      "p",
                      null,
                      (card.decision_summary ? card.decision_summary.count : 0)
                        + " confirmed decision"
                        + ((card.decision_summary && card.decision_summary.count === 1) ? "" : "s"),
                    ),
                    card.decision_summary && card.decision_summary.latest
                      ? renderDecision(
                          card.decision_summary.latest,
                          "latest",
                          "space-y-1 rounded-md border p-2",
                        )
                      : null,
                  ),
                  React.createElement(
                    "section",
                    { className: "space-y-1", "aria-label": "Chairman approval status" },
                    React.createElement(
                      "p",
                      null,
                      ((card.approval_summary || {}).count || 0)
                        + " approval request"
                        + (((card.approval_summary || {}).count === 1) ? "" : "s"),
                    ),
                    (card.approval_summary || {}).pending_count
                      ? React.createElement(
                          Badge,
                          { variant: "outline" },
                          card.approval_summary.pending_count + " pending chairman decision",
                        )
                      : null,
                    (card.approval_summary || {}).latest
                      ? React.createElement(
                          "p",
                          null,
                          "Latest: " + card.approval_summary.latest.status
                            + " · " + card.approval_summary.latest.request_id,
                        )
                      : null,
                  ),
                  React.createElement(
                    "section",
                    { className: "space-y-2", "aria-label": "PM executive summary" },
                    (card.delivery_summary || {}).state === "reported"
                      ? React.createElement(
                          "p",
                          null,
                          card.delivery_summary.count + " PM executive report"
                            + (card.delivery_summary.count === 1 ? "" : "s"),
                        )
                      : React.createElement("p", null, "PM delivery not reported"),
                    ((card.delivery_summary || {}).badges || []).length
                      ? React.createElement(
                          "div",
                          { className: "flex flex-wrap gap-1", "aria-label": "PM report badges" },
                          card.delivery_summary.badges.map(function (badge) {
                            return React.createElement(
                              Badge,
                              { key: badge.type, variant: "outline" },
                              readableBadge(badge.type) + " · " + badge.count,
                            );
                          }),
                        )
                      : null,
                    (card.delivery_summary || {}).latest
                      ? renderPMReport(
                          card.delivery_summary.latest,
                          "latest-pm-report",
                          "space-y-1 rounded-md border p-2",
                        )
                      : null,
                  ),
                  React.createElement(
                    "section",
                    { className: "space-y-1", "aria-label": "External effect delivery" },
                    React.createElement(
                      "p",
                      null,
                      (card.external_effects || {}).state === "needs_repair"
                        ? "External effects need operator repair"
                        : (card.external_effects || {}).state === "retrying"
                          ? "External effects retrying"
                          : (card.external_effects || {}).state === "in_progress"
                            ? "External effects in progress"
                            : "External effects healthy",
                    ),
                    (card.external_effects || {}).retry_scheduled_count
                      ? React.createElement(
                          Badge,
                          { variant: "outline" },
                          card.external_effects.retry_scheduled_count + " retry scheduled",
                        )
                      : null,
                    (card.external_effects || {}).latest_terminal
                      ? React.createElement(
                          "div",
                          { className: "space-y-1 rounded-md border p-2" },
                          React.createElement(
                            "p",
                            { className: "font-medium text-foreground" },
                            card.external_effects.latest_terminal.terminal_outcome.message,
                          ),
                          React.createElement(
                            "p",
                            null,
                            "Repair action: maps outbox repair --effect "
                              + card.external_effects.latest_terminal.effect_id,
                          ),
                        )
                      : null,
                  ),
                  detailState.mapId === card.id && detailState.status === "ready"
                    ? React.createElement(
                        "section",
                        {
                          className: "space-y-2 rounded-md border p-3",
                          "aria-label": "Map detail",
                        },
                        React.createElement(
                          "h3",
                          { className: "font-medium text-foreground" },
                          "Recent confirmed decisions",
                        ),
                        (detailState.detail.recent_decisions || []).length === 0
                          ? React.createElement("p", null, "No confirmed decisions")
                          : (detailState.detail.recent_decisions || []).map(
                              function (decision) {
                                return renderDecision(
                                  decision,
                                  decision.decision_id,
                                  "space-y-1",
                                );
                              },
                            ),
                        React.createElement(
                          "section",
                          { className: "space-y-2", "aria-label": "Chairman approval packets" },
                          React.createElement(
                            "h3",
                            { className: "font-medium text-foreground" },
                            "Chairman approval packets",
                          ),
                          ((detailState.detail.approvals || {}).items || []).length === 0
                            ? React.createElement("p", null, "No approval requests")
                            : (detailState.detail.approvals.items || []).map(
                                function (approval) {
                                  return React.createElement(
                                    "article",
                                    {
                                      key: approval.request_id,
                                      className: "space-y-2 rounded-md border p-3",
                                      "aria-label": "Approval " + approval.request_id,
                                    },
                                    React.createElement(
                                      "div",
                                      { className: "flex flex-wrap items-center gap-2" },
                                      React.createElement(
                                        "p",
                                        { className: "font-medium text-foreground" },
                                        approval.proposed_action + " · " + approval.request_id,
                                      ),
                                      React.createElement(Badge, { variant: "outline" }, approval.status),
                                    ),
                                    React.createElement("p", null, approval.rationale),
                                    React.createElement(
                                      "div",
                                      null,
                                      React.createElement(
                                        "p",
                                        { className: "font-medium text-foreground" },
                                        "Alternatives",
                                      ),
                                      React.createElement(
                                        "ul",
                                        { className: "list-disc space-y-1 pl-5" },
                                        (approval.alternatives || []).map(function (alternative, index) {
                                          return React.createElement("li", { key: index }, alternative);
                                        }),
                                      ),
                                    ),
                                    React.createElement(
                                      "p",
                                      null,
                                      "Cost / risk: " + approval.cost_risk,
                                    ),
                                    React.createElement(
                                      "p",
                                      null,
                                      "Scope: ",
                                      React.createElement(
                                        "code",
                                        null,
                                        JSON.stringify(approval.requested_scope),
                                      ),
                                    ),
                                    React.createElement(
                                      "p",
                                      null,
                                      "Decision content: ",
                                      React.createElement(
                                        "code",
                                        null,
                                        JSON.stringify(approval.decision_payload),
                                      ),
                                    ),
                                    React.createElement(
                                      "p",
                                      null,
                                      "Payload hash: ",
                                      React.createElement("code", null, approval.payload_hash),
                                    ),
                                    approval.expires_at
                                      ? React.createElement(
                                          "p",
                                          null,
                                          "Expires: ",
                                          React.createElement(
                                            "time",
                                            { dateTime: approval.expires_at },
                                            approval.expires_at,
                                          ),
                                        )
                                      : React.createElement("p", null, "Expiry starts on approval"),
                                    React.createElement(
                                      "ul",
                                      { className: "list-disc space-y-1 pl-5", "aria-label": "Approval evidence" },
                                      (approval.evidence || []).map(function (evidence, index) {
                                        const href = safeExternalUrl(evidence);
                                        return React.createElement(
                                          "li",
                                          { key: index },
                                          href
                                            ? React.createElement(
                                                "a",
                                                {
                                                  href: href,
                                                  target: "_blank",
                                                  rel: "noreferrer",
                                                  className: "underline-offset-4 hover:underline",
                                                },
                                                evidence,
                                              )
                                            : evidence,
                                        );
                                      }),
                                    ),
                                    approval.status === "pending"
                                      ? React.createElement(
                                          "div",
                                          {
                                            className: "flex flex-wrap gap-2",
                                            "aria-label": "Chairman approval decisions",
                                          },
                                          [
                                            ["approved", "Approve"],
                                            ["rejected", "Reject"],
                                            ["revision", "Request revision"],
                                          ].map(function (choice) {
                                            const pending = approvalState.status === "pending"
                                              && approvalState.requestId === approval.request_id;
                                            return React.createElement(
                                              Button,
                                              {
                                                key: choice[0],
                                                type: "button",
                                                variant: choice[0] === "approved" ? "default" : "outline",
                                                disabled: pending,
                                                "aria-label": choice[1] + " " + approval.request_id,
                                                onClick: function () {
                                                  decideApproval(
                                                    card.id,
                                                    approval.request_id,
                                                    choice[0],
                                                  );
                                                },
                                              },
                                              pending ? "Recording decision…" : choice[1],
                                            );
                                          }),
                                        )
                                      : null,
                                  );
                                },
                              ),
                        ),
                        React.createElement(
                          "section",
                          { className: "space-y-2", "aria-label": "PM executive reports" },
                          React.createElement(
                            "h3",
                            { className: "font-medium text-foreground" },
                            "PM executive reports",
                          ),
                          (detailState.detail.pm_reports || []).length === 0
                            ? React.createElement("p", null, "No PM executive reports")
                            : detailState.detail.pm_reports.map(function (report) {
                                return renderPMReport(
                                  report,
                                  report.record_id,
                                  "space-y-1 rounded-md border p-2",
                                );
                              }),
                        ),
                        React.createElement(
                          "p",
                          null,
                          "Delivery: "
                            + ((detailState.detail.delivery_summary || {}).state
                              || "not_reported"),
                        ),
                      )
                    : null,
                  detailState.mapId === card.id && detailState.status === "error"
                    ? React.createElement(
                        "p",
                        { role: "alert", className: "text-sm text-destructive" },
                        detailState.message,
                      )
                    : null,
                  React.createElement(
                    "p",
                    null,
                    "CEO session: " + card.ceo_session.state,
                  ),
                  card.ceo_session.last_activity_at
                    ? React.createElement(
                        "p",
                        null,
                        "CEO last activity: ",
                        React.createElement(
                          "time",
                          { dateTime: card.ceo_session.last_activity_at },
                          card.ceo_session.last_activity_at,
                        ),
                      )
                    : null,
                  card.ceo_session.repair
                    ? React.createElement(
                        "p",
                        { role: "alert", className: "text-sm text-destructive" },
                        "Session repair required: "
                          + card.ceo_session.repair.reason
                          + " ("
                          + card.ceo_session.repair.candidate_count
                          + " exact candidates)",
                      )
                    : null,
                  React.createElement(
                    "p",
                    null,
                    "Last synchronized: ",
                    React.createElement(
                      "time",
                      { dateTime: card.last_synchronized_at },
                      card.last_synchronized_at,
                    ),
                  ),
                  transitionState.status === "error" && transitionState.mapId === card.id
                    ? React.createElement(
                        "p",
                        { role: "alert", className: "text-sm text-destructive" },
                        transitionState.message,
                      )
                    : null,
                  sessionState.status === "error" && sessionState.mapId === card.id
                    ? React.createElement(
                        "p",
                        { role: "alert", className: "text-sm text-destructive" },
                        sessionState.message,
                      )
                    : null,
                  approvalState.status === "error" && approvalState.mapId === card.id
                    ? React.createElement(
                        "p",
                        { role: "alert", className: "text-sm text-destructive" },
                        approvalState.message,
                      )
                    : null,
                  React.createElement(
                    "div",
                    { className: "flex flex-wrap gap-2", "aria-label": "Map actions" },
                    React.createElement(
                      Button,
                      {
                        type: "button",
                        disabled: card.ceo_session.state === "repair_required"
                          || (sessionState.status === "pending" && sessionState.mapId === card.id),
                        onClick: function () { openCEOSession(card.id); },
                      },
                      card.ceo_session.state === "repair_required"
                        ? "Repair required"
                        : "Open CEO session",
                    ),
                    React.createElement(
                      Button,
                      {
                        type: "button",
                        variant: "outline",
                        disabled: detailState.status === "loading"
                          && detailState.mapId === card.id,
                        onClick: function () { loadMapDetail(card.id); },
                      },
                      detailState.status === "loading" && detailState.mapId === card.id
                        ? "Loading Map detail…"
                        : "View Map detail",
                    ),
                    (card.available_transitions || []).map(function (stage) {
                      const pending = transitionState.status === "pending" && transitionState.mapId === card.id;
                      return React.createElement(
                        Button,
                        {
                          key: stage,
                          type: "button",
                          variant: "outline",
                          disabled: pending,
                          onClick: function () { transitionMap(card.id, card.stage, stage); },
                        },
                        "Move to " + stage,
                      );
                    }),
                  ),
                ),
              );
            }),
          ),
        );
      }),
    );
  }

  registry.register("map-governance", MapsPage);
})();
