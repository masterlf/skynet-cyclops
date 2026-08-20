(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const React = SDK.React;
  const h = React.createElement;
  const STATUS_URL = "/api/plugins/skynet-cyclops/status";

  function label(value) {
    return value === null || value === undefined ? "unknown" : String(value);
  }

  function StatusList(props) {
    const items = props.items || [];
    if (items.length === 0) {
      return h("p", { className: "cyclops-muted" }, props.empty);
    }
    return h("ul", { className: "cyclops-list" }, ...items.map(props.render));
  }

  function Mission(props) {
    const mission = props.mission;
    return h("article", { className: "cyclops-card" },
      h("h3", {}, mission.id),
      h("p", {}, "Outcome: ", h("strong", {}, mission.outcome), " · Next: ", label(mission.next_phase)),
      h("h4", {}, "Phases"),
      h(StatusList, {
        items: mission.phases,
        empty: "No phase data",
        render: (phase) => h("li", { key: phase.key }, phase.key, ": ", phase.state,
          " · evidence: ", (phase.evidence_present || []).join(", ") || "none")
      }),
      h("h4", {}, "Workers"),
      h(StatusList, {
        items: mission.workers,
        empty: "No active workers",
        render: (worker) => h("li", { key: worker.run_id }, worker.assignee, " · ", worker.status,
          " · heartbeat age: ", label(worker.heartbeat_age_seconds), "s · retries: ", label(worker.retry_count))
      })
    );
  }

  function Component() {
    const state = React.useState({ loading: true, data: null, error: false });
    const view = state[0];
    const setView = state[1];
    React.useEffect(function () {
      let mounted = true;
      SDK.fetchJSON(STATUS_URL).then(function (data) {
        if (mounted) setView({ loading: false, data: data, error: false });
      }).catch(function () {
        if (mounted) setView({ loading: false, data: null, error: true });
      });
      return function () { mounted = false; };
    }, []);

    if (view.loading) {
      return h("section", { "aria-label": "Skynet-Cyclops read-only status", className: "cyclops-panel" },
        h("p", { "aria-live": "polite" }, "Loading observer status…"));
    }
    if (view.error || !view.data) {
      return h("section", { "aria-label": "Skynet-Cyclops read-only status", className: "cyclops-panel" },
        h("div", { role: "alert", className: "cyclops-alert" }, "Observer status is unavailable."));
    }
    const data = view.data;
    return h("section", { "aria-label": "Skynet-Cyclops read-only status", className: "cyclops-panel" },
      data.supervisor.state !== "ok" || data.supervisor.post_gap
        ? h("div", { role: "status", className: "cyclops-alert" },
            "Supervisor: ", data.supervisor.state,
            data.supervisor.post_gap ? " · first report after a collection gap" : "")
        : null,
      h("header", {}, h("h2", {}, "Skynet-Cyclops"),
        h("p", {}, "Observe-only · tick ", label(data.supervisor.tick_seq),
          " · cost: ", data.cost.classification)),
      h(StatusList, {
        items: data.missions,
        empty: "No configured missions",
        render: (mission) => h(Mission, { key: mission.id, mission: mission })
      }),
      h("h3", {}, "Incidents"),
      h(StatusList, {
        items: data.incidents,
        empty: "No incidents",
        render: (incident) => h("li", { key: incident.id + ":" + label(incident.generation) },
          incident.severity, " · ", incident.phase_key, " · ", incident.kind,
          " · ", incident.lifecycle || incident.disposition,
          incident.generation ? " · generation " + label(incident.generation) : "",
          incident.manager_state ? " · manager " + incident.manager_state : "",
          incident.notification_state ? " · notification " + incident.notification_state : "",
          " · age ", label(incident.age_ticks), " ticks")
      })
    );
  }

  window.__HERMES_PLUGINS__.register("skynet-cyclops", Component);
}());
