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
            null,
            React.createElement(
              "p",
              { className: "text-sm text-muted-foreground" },
              state.board.empty_state.description,
            ),
          ),
        ),
      );
    }

    return React.createElement(
      "div",
      { className: "p-6" },
      React.createElement("h1", { className: "text-xl font-semibold" }, "Maps"),
    );
  }

  registry.register("map-governance", MapsPage);
})();
