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

  function MapsPage() {
    const [state, setState] = useState({ status: "loading" });

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
                  { className: "space-y-1 text-xs text-muted-foreground" },
                  React.createElement(
                    "p",
                    null,
                    "CEO session: " + card.ceo_session.state,
                  ),
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
