#include <pybind11/pybind11.h>
#include <pybind11/functional.h>
#include <pybind11/stl.h>

#include <limits>
#include <sstream>
#include <string>

#include "maabangdream/chart_timeline.hpp"
#include "maabangdream/minitouch_client.hpp"
#include "maabangdream/minitouch_log.hpp"
#include "maabangdream/playback_session.hpp"
#include "maabangdream/pure_chart.hpp"
#include "maabangdream/scheduler.hpp"
#include "maabangdream/song_clock.hpp"
#include "maabangdream/touch_script.hpp"
#include "maabangdream/types.hpp"
#include "maabangdream/version.hpp"

namespace py = pybind11;
namespace mbdr {

namespace {

// 把 ScheduledAction 转成 Python dict，字段与 Python 侧 TouchAction 语义
// 对齐（contact=-1 表示由 Python 派发时分配）。
py::dict action_to_dict(const ScheduledAction& action) {
    py::dict result;
    result["kind"] = action_kind_name(action.kind);
    result["lane"] = static_cast<int>(action.lane);
    result["contact"] = static_cast<int>(action.contact);
    result["target_x"] = action.target_x;
    result["due_s"] = action.due_s;
    result["note_index"] = action.note_index;
    py::object direction_value = action.flick_direction == 0
        ? py::object(py::none())
        : py::object(py::str(action.flick_direction < 0 ? "Left" : "Right"));
    result["flick_direction"] = direction_value;
    return result;
}

py::dict judgement_to_dict(const ChartJudgement& judgement) {
    py::dict result;
    result["time_s"] = judgement.time_s;
    result["lane"] = static_cast<int>(judgement.lane);
    result["kind"] = judgement_kind_name(judgement.kind);
    result["note_index"] = judgement.note_index;
    result["flick"] = judgement.flick;
    py::object direction_value = judgement.direction == 0
        ? py::object(py::none())
        : py::object(py::str(judgement.direction < 0 ? "Left" : "Right"));
    result["direction"] = direction_value;
    result["tail_flick"] = judgement.tail_flick;
    return result;
}

py::dict config_to_dict(const EngineConfig& config) {
    py::dict result;
    result["judgement_y"] = config.judgement_y;
    result["press_bias_ms"] = config.press_bias_ms;
    result["max_wait_ms"] = config.max_wait_ms;
    result["tap_duration_ms"] = config.tap_duration_ms;
    result["flick_duration_ms"] = config.flick_duration_ms;
    result["slide_step_s"] = config.slide_step_s;
    result["song_offset_s"] = config.song_offset_s;
    py::list centers;
    for (const float center : config.lane_centers) {
        centers.append(center);
    }
    result["lane_centers"] = centers;
    return result;
}

ScheduledAction action_from_dict(const py::dict& item) {
    ScheduledAction action;
    const std::string kind = item["kind"].cast<std::string>();
    if (kind == "tap") {
        action.kind = ActionKind::Tap;
    } else if (kind == "flick") {
        action.kind = ActionKind::Flick;
    } else if (kind == "down") {
        action.kind = ActionKind::Down;
    } else if (kind == "move") {
        action.kind = ActionKind::Move;
    } else if (kind == "up") {
        action.kind = ActionKind::Up;
    } else {
        throw std::invalid_argument("unknown action kind: " + kind);
    }
    action.lane = static_cast<uint8_t>(item["lane"].cast<int>());
    action.contact = static_cast<int8_t>(item["contact"].cast<int>());
    action.target_x = item["target_x"].cast<float>();
    action.due_s = item["due_s"].cast<double>();
    if (item.contains("note_index")) {
        action.note_index = item["note_index"].cast<int>();
    }
    const py::object direction = item["flick_direction"];
    if (!direction.is_none()) {
        const std::string text = direction.cast<std::string>();
        action.flick_direction = static_cast<int8_t>(text == "Left" ? -1 : 1);
    }
    return action;
}

EngineConfig config_from_dict(const py::dict& source) {
    EngineConfig config;
    if (source.contains("judgement_y")) {
        config.judgement_y = source["judgement_y"].cast<double>();
    }
    if (source.contains("press_bias_ms")) {
        config.press_bias_ms = source["press_bias_ms"].cast<int>();
    }
    if (source.contains("max_wait_ms")) {
        config.max_wait_ms = source["max_wait_ms"].cast<int>();
    }
    if (source.contains("tap_duration_ms")) {
        config.tap_duration_ms = source["tap_duration_ms"].cast<int>();
    }
    if (source.contains("flick_duration_ms")) {
        config.flick_duration_ms =
            source["flick_duration_ms"].cast<int>();
    }
    if (source.contains("slide_step_s")) {
        config.slide_step_s = source["slide_step_s"].cast<double>();
    }
    if (source.contains("song_offset_s")) {
        config.song_offset_s = source["song_offset_s"].cast<double>();
    }
    if (source.contains("lane_centers")) {
        const auto centers = source["lane_centers"].cast<std::vector<float>>();
        if (centers.size() != kLaneCount) {
            throw std::invalid_argument(
                "lane_centers must contain exactly 7 entries");
        }
        for (std::size_t index = 0; index < centers.size(); ++index) {
            config.lane_centers[index] = centers[index];
        }
    }
    return config;
}

SyncConfig sync_config_from_dict(const py::dict& source) {
    SyncConfig config;
    if (source.contains("match_tol_s")) {
        config.match_tol_s = source["match_tol_s"].cast<double>();
    }
    if (source.contains("max_mad_s")) {
        config.max_mad_s = source["max_mad_s"].cast<double>();
    }
    if (source.contains("min_margin_s")) {
        config.min_margin_s = source["min_margin_s"].cast<double>();
    }
    if (source.contains("min_samples")) {
        config.min_samples = source["min_samples"].cast<int>();
    }
    if (source.contains("min_samples_with_anchor")) {
        config.min_samples_with_anchor =
            source["min_samples_with_anchor"].cast<int>();
    }
    if (source.contains("min_margin_samples")) {
        config.min_margin_samples = source["min_margin_samples"].cast<int>();
    }
    if (source.contains("min_lanes")) {
        config.min_lanes = source["min_lanes"].cast<int>();
    }
    if (source.contains("prelude_grace_s")) {
        config.prelude_grace_s = source["prelude_grace_s"].cast<double>();
    }
    if (source.contains("anchor_default_uncertainty_s")) {
        config.anchor_default_uncertainty_s =
            source["anchor_default_uncertainty_s"].cast<double>();
    }
    if (source.contains("min_offset_s")) {
        config.min_offset_s = source["min_offset_s"].cast<double>();
    }
    if (source.contains("max_offset_s")) {
        config.max_offset_s = source["max_offset_s"].cast<double>();
    }
    if (source.contains("offset_step_s")) {
        config.offset_step_s = source["offset_step_s"].cast<double>();
    }
    if (source.contains("sync_chart_window_s")) {
        config.sync_chart_window_s =
            source["sync_chart_window_s"].cast<double>();
    }
    return config;
}

py::dict sync_state_to_dict(const SyncState& state) {
    using namespace pybind11::literals;
    py::dict result;
    switch (state.status) {
        case SyncState::Status::Pending:
            result["status"] = "pending";
            break;
        case SyncState::Status::Locked:
            result["status"] = "locked";
            break;
        case SyncState::Status::Rejected:
            result["status"] = "rejected";
            break;
    }
    result["offset_s"] = state.offset_s;
    result["samples"] = state.samples;
    result["lanes"] = state.lanes;
    result["mad_s"] = state.mad_s;
    result["median_residual_s"] = state.median_residual_s;
    result["locked_at_s"] = state.locked_at_s;
    result["has_anchor"] = state.has_anchor;
    result["anchor_time_s"] = state.anchor_time_s;
    result["anchor_uncertainty_s"] = state.anchor_uncertainty_s;
    result["best"] = py::dict(
        "offset_s"_a = state.best.offset_s,
        "matches"_a = state.best.matches,
        "lanes"_a = state.best.lanes,
        "median_residual_s"_a = state.best.median_residual_s,
        "mad_s"_a = state.best.mad_s,
        "first_matched_obs_s"_a = state.best.first_matched_obs_s,
        "last_matched_obs_s"_a = state.best.last_matched_obs_s);
    result["second"] = py::dict(
        "offset_s"_a = state.second.offset_s,
        "matches"_a = state.second.matches,
        "lanes"_a = state.second.lanes,
        "mad_s"_a = state.second.mad_s);
    result["best_second_offset_gap_s"] = state.best_second_offset_gap_s;
    result["second_matches"] = state.second_matches;
    result["reason"] = state.reason;
    return result;
}

PlaybackSessionConfig playback_config_from_dict(const py::dict& source) {
    PlaybackSessionConfig config;
    if (source.contains("lookahead_s")) {
        config.lookahead_s = source["lookahead_s"].cast<double>();
    }
    if (source.contains("low_water_s")) {
        config.low_water_s = source["low_water_s"].cast<double>();
    }
    if (source.contains("max_queue_s")) {
        config.max_queue_s = source["max_queue_s"].cast<double>();
    }
    if (source.contains("reset_timeout_s")) {
        config.reset_timeout_s =
            source["reset_timeout_s"].cast<double>();
    }
    if (source.contains("cancel_deadline_s")) {
        config.cancel_deadline_s =
            source["cancel_deadline_s"].cast<double>();
    }
    return config;
}

py::dict playback_chunk_to_dict(const PlaybackChunk& chunk) {
    py::dict result;
    result["sequence"] = chunk.sequence;
    result["window_start_s"] = chunk.window_start_s;
    result["window_end_s"] = chunk.window_end_s;
    result["final_chunk"] = chunk.final_chunk;
    result["touch_config"] = config_to_dict(chunk.touch_config);
    py::list actions;
    for (const TimedPlaybackAction& timed : chunk.actions) {
        py::dict entry;
        entry["action"] = action_to_dict(timed.action);
        entry["engine_due_s"] = timed.engine_due_s;
        actions.append(std::move(entry));
    }
    result["actions"] = std::move(actions);
    py::list reservations;
    for (const TimedPlaybackAction& timed
         : chunk.future_down_reservations) {
        py::dict entry;
        entry["action"] = action_to_dict(timed.action);
        entry["engine_due_s"] = timed.engine_due_s;
        reservations.append(std::move(entry));
    }
    result["future_down_reservations"] = std::move(reservations);
    return result;
}

const char* playback_state_name(PlaybackState state) noexcept {
    switch (state) {
        case PlaybackState::Idle:
            return "idle";
        case PlaybackState::Armed:
            return "armed";
        case PlaybackState::Running:
            return "running";
        case PlaybackState::Cancelling:
            return "cancelling";
        case PlaybackState::Finished:
            return "finished";
        case PlaybackState::Cancelled:
            return "cancelled";
        case PlaybackState::Failed:
            return "failed";
    }
    return "unknown";
}

py::dict playback_report_to_dict(const PlaybackReport& report) {
    py::dict result;
    result["planned"] = report.planned_actions;
    result["sent"] = report.sent_actions;
    result["executed"] = report.executed_actions;
    result["chunks"] = report.chunks;
    result["underflows"] = report.queue_underflows;
    result["tap_actions"] = report.tap_actions;
    result["flick_actions"] = report.flick_actions;
    result["hold_starts"] = report.hold_starts;
    result["hold_moves"] = report.hold_moves;
    result["hold_releases"] = report.hold_releases;
    result["chord_groups"] = report.chord_groups;
    result["probe_events"] = report.probe_events;
    result["chart_first_due_s"] = report.chart_first_due_s;
    result["first_action_engine_s"] = report.first_action_engine_s;
    result["max_queue_depth_ms"] = report.max_queue_depth_ms;
    result["drift_p50_ms"] = report.drift_p50_ms;
    result["drift_p95_ms"] = report.drift_p95_ms;
    result["drift_max_ms"] = report.drift_max_ms;
    result["stop_latency_ms"] = report.stop_latency_ms;
    result["fallback_used"] = report.fallback_used;
    result["reason"] = report.terminal_reason;
    return result;
}

const char* touch_command_name(TouchCommandKind command) noexcept {
    switch (command) {
        case TouchCommandKind::Down:
            return "d";
        case TouchCommandKind::Move:
            return "m";
        case TouchCommandKind::Up:
            return "u";
    }
    return "";
}

py::list execution_receipts_to_list(const TouchScriptCompiler& compiler) {
    py::list result;
    for (const TouchExecutionReceipt& receipt
         : compiler.last_execution_receipts()) {
        py::dict item;
        item["line_index"] = receipt.line_index;
        item["planned_engine_s"] = receipt.planned_engine_s;
        item["action_token"] = receipt.action_token;
        item["command"] = touch_command_name(receipt.command);
        result.append(std::move(item));
    }
    return result;
}

MinitouchLogEvent log_event_from_dict(const py::dict& event_dict) {
    MinitouchLogEvent event;
    event.start_ms = event_dict["start_ms"].cast<double>();
    event.end_ms = event_dict["end_ms"].cast<double>();
    event.cost_ms = event_dict["cost_ms"].cast<double>();
    event.command = py::str(event_dict["command"]).cast<std::string>();
    return event;
}

}  // namespace

}  // namespace mbdr

PYBIND11_MODULE(maabangdream_realtime, module) {
    using namespace mbdr;
    using namespace pybind11::literals;
    module.doc() =
        "MaaBanGDream Native Realtime Engine V2 (Pure Chart / Scheduler / Sync)";
    module.def("version", []() { return std::string(kNativeVersion); });

    py::class_<ChartTimeline>(module, "ChartTimeline")
        .def(py::init<>())
        .def_static("from_file", &ChartTimeline::from_json_file,
            py::arg("path"))
        .def_static("from_json", &ChartTimeline::from_json_string,
            py::arg("text"))
        .def_property_readonly("judgement_count",
            [](const ChartTimeline& self) {
                return static_cast<int>(self.judgements.size());
            })
        .def_property_readonly("hold_count",
            [](const ChartTimeline& self) {
                return static_cast<int>(self.hold_paths.size());
            })
        .def_property_readonly("start_time_s",
            [](const ChartTimeline& self) { return self.start_time_s; })
        .def_property_readonly("end_time_s",
            [](const ChartTimeline& self) { return self.end_time_s; })
        .def_property_readonly("bestdori_song_id",
            [](const ChartTimeline& self) { return self.bestdori_song_id; })
        .def_property_readonly("difficulty",
            [](const ChartTimeline& self) { return self.difficulty; })
        .def_property_readonly("level",
            [](const ChartTimeline& self) { return self.level; })
        .def("judgements",
            [](const ChartTimeline& self) {
                py::list result;
                for (const ChartJudgement& judgement : self.judgements) {
                    result.append(judgement_to_dict(judgement));
                }
                return result;
            })
        .def("compile_actions",
            [](const ChartTimeline& self, py::dict config_dict) {
                const EngineConfig config = config_from_dict(config_dict);
                const auto actions =
                    compile_pure_chart_actions(self, config);
                py::list result;
                for (const ScheduledAction& action : actions) {
                    result.append(action_to_dict(action));
                }
                return result;
            },
            py::arg("config") = py::dict());

    py::class_<ActionScheduler>(module, "ActionScheduler")
        .def(py::init([](py::list actions, py::dict config_dict) {
            EngineConfig config = config_from_dict(config_dict);
            std::vector<ScheduledAction> parsed;
            parsed.reserve(actions.size());
            for (const auto handle : actions) {
                parsed.push_back(action_from_dict(
                    handle.cast<py::dict>()));
            }
            return std::make_unique<ActionScheduler>(
                std::move(parsed), std::move(config));
        }), py::arg("actions"), py::arg("config") = py::dict())
        .def("tick",
            [](ActionScheduler& self, double now_s) {
                const auto due = self.tick(now_s);
                py::list result;
                for (const ScheduledAction& action : due) {
                    result.append(action_to_dict(action));
                }
                return result;
            },
            py::arg("now_s"))
        .def("stop",
            [](ActionScheduler& self) {
                const auto releases = self.stop();
                py::list result;
                for (const ScheduledAction& action : releases) {
                    result.append(action_to_dict(action));
                }
                return result;
            })
        .def("stats",
            [](const ActionScheduler& self) {
                const SchedulerStats& stats = self.stats();
                py::dict result;
                result["dispatched"] = stats.dispatched;
                result["late_count"] = stats.late_count;
                result["late_max_ms"] = stats.late_max_ms;
                result["late_p50_ms"] = stats.late_p50_ms;
                result["late_p95_ms"] = stats.late_p95_ms;
                result["scheduled_total"] = stats.scheduled_total;
                return result;
            })
        .def_property_readonly("stopped",
            [](const ActionScheduler& self) { return self.stopped(); });

    py::class_<SongClockSynchronizer>(module, "SongClockSynchronizer")
        .def(py::init([](const ChartTimeline& chart, py::dict config_dict) {
            return std::make_unique<SongClockSynchronizer>(
                chart, sync_config_from_dict(config_dict));
        }), py::arg("chart"), py::arg("config") = py::dict())
        .def("set_anchor",
            &SongClockSynchronizer::set_anchor,
            py::arg("engine_time_s"),
            py::arg("uncertainty_s") = 0.6)
        .def("observe",
            [](SongClockSynchronizer& self, int lane, const std::string& kind,
               double engine_time_s) {
                SyncObservation observation;
                observation.lane = static_cast<uint8_t>(lane);
                observation.engine_time_s = engine_time_s;
                if (kind == "tap") {
                    observation.kind = NoteKind::Tap;
                } else if (kind == "flick") {
                    observation.kind = NoteKind::Flick;
                } else if (kind == "skill") {
                    observation.kind = NoteKind::Skill;
                } else if (kind == "hold") {
                    observation.kind = NoteKind::Hold;
                } else {
                    throw std::invalid_argument("unknown observation kind: " + kind);
                }
                self.observe(observation);
            },
            py::arg("lane"), py::arg("kind"), py::arg("engine_time_s"))
        .def("reject",
            &SongClockSynchronizer::reject,
            py::arg("reason"))
        .def("state",
            [](const SongClockSynchronizer& self) {
                return sync_state_to_dict(self.state());
            });

    py::class_<TouchLatencyOffsets>(module, "TouchLatencyOffsets")
        .def(py::init([](double down_ms, double up_ms, double move_ms,
                         double wait_ms, double interval_ms) {
            TouchLatencyOffsets offsets;
            offsets.down_ms = down_ms;
            offsets.up_ms = up_ms;
            offsets.move_ms = move_ms;
            offsets.wait_ms = wait_ms;
            offsets.interval_ms = interval_ms;
            return offsets;
        }),
        py::arg("down_ms") = 0,
        py::arg("up_ms") = 0,
        py::arg("move_ms") = 0,
        py::arg("wait_ms") = 0,
        py::arg("interval_ms") = 0)
        .def_readwrite("down_ms", &TouchLatencyOffsets::down_ms)
        .def_readwrite("up_ms", &TouchLatencyOffsets::up_ms)
        .def_readwrite("move_ms", &TouchLatencyOffsets::move_ms)
        .def_readwrite("wait_ms", &TouchLatencyOffsets::wait_ms)
        .def_readwrite("interval_ms", &TouchLatencyOffsets::interval_ms);

    py::class_<TouchScriptCompiler>(module, "TouchScriptCompiler")
        .def(py::init([](const TouchLatencyOffsets& offsets) {
            return std::make_unique<TouchScriptCompiler>(offsets);
        }), py::arg("offsets") = TouchLatencyOffsets{})
        .def("set_offsets", &TouchScriptCompiler::set_offsets,
            py::arg("offsets"))
        .def_property_readonly("offsets", &TouchScriptCompiler::offsets)
        .def("add_residual_ms", &TouchScriptCompiler::add_residual_ms,
            py::arg("ms"))
        .def("reset_contacts", &TouchScriptCompiler::reset_contacts)
        .def("execution_receipts", &execution_receipts_to_list)
        .def("last_execution_receipts", &execution_receipts_to_list)
        .def("compile",
            [](TouchScriptCompiler& self, py::list actions,
               py::dict config_dict, double start_engine_time,
               bool final_chunk, double end_engine_time,
               py::list future_down_reservations) {
                std::vector<ScheduledAction> parsed;
                parsed.reserve(actions.size());
                for (const auto handle : actions) {
                    parsed.push_back(action_from_dict(
                        handle.cast<py::dict>()));
                }
                std::vector<ScheduledAction> reservations;
                reservations.reserve(future_down_reservations.size());
                for (const auto handle : future_down_reservations) {
                    reservations.push_back(action_from_dict(
                        handle.cast<py::dict>()));
                }
                const std::vector<std::string> lines = self.compile(
                    std::move(parsed),
                    config_from_dict(config_dict),
                    start_engine_time,
                    final_chunk,
                    end_engine_time,
                    std::move(reservations));
                py::list result;
                for (const std::string& text : lines) {
                    result.append(text);
                }
                return result;
            },
            py::arg("actions"),
            py::arg("config") = py::dict(),
            py::arg("start_engine_time") = 0.0,
            py::arg("final_chunk") = true,
            py::arg("end_engine_time") =
                std::numeric_limits<double>::quiet_NaN(),
            py::arg("future_down_reservations") = py::list());

    py::class_<PlaybackSession>(module, "PlaybackSession")
        .def(py::init([](py::function publish,
                         py::object request_reset,
                         py::object fallback_stop,
                         py::object clock,
                         py::dict config_dict) {
            PlaybackCallbacks callbacks;
            callbacks.publish =
                [callback = std::move(publish)](
                    const PlaybackChunk& chunk) -> bool {
                    py::gil_scoped_acquire acquire;
                    return callback(playback_chunk_to_dict(chunk))
                        .cast<bool>();
                };
            if (!request_reset.is_none()) {
                py::function callback = request_reset.cast<py::function>();
                callbacks.request_reset =
                    [callback = std::move(callback)]() -> bool {
                        py::gil_scoped_acquire acquire;
                        return callback().cast<bool>();
                    };
            }
            if (!fallback_stop.is_none()) {
                py::function callback = fallback_stop.cast<py::function>();
                callbacks.fallback_stop =
                    [callback = std::move(callback)]() -> bool {
                        py::gil_scoped_acquire acquire;
                        return callback().cast<bool>();
                    };
            }
            if (!clock.is_none()) {
                py::function callback = clock.cast<py::function>();
                callbacks.clock =
                    [callback = std::move(callback)]() -> double {
                        py::gil_scoped_acquire acquire;
                        return callback().cast<double>();
                    };
            }
            return std::make_unique<PlaybackSession>(
                std::move(callbacks),
                playback_config_from_dict(config_dict));
        }),
        py::arg("publish"),
        py::arg("request_reset") = py::none(),
        py::arg("fallback_stop") = py::none(),
        py::arg("clock") = py::none(),
        py::arg("config") = py::dict())
        .def("arm",
            [](PlaybackSession& self, py::list actions,
               py::dict config_dict) {
                std::vector<ScheduledAction> parsed;
                parsed.reserve(actions.size());
                for (const auto handle : actions) {
                    parsed.push_back(action_from_dict(
                        handle.cast<py::dict>()));
                }
                return self.arm(
                    std::move(parsed),
                    config_from_dict(config_dict));
            },
            py::arg("actions"),
            py::arg("engine_config") = py::dict())
        .def("start", &PlaybackSession::start,
            py::arg("first_action_engine_s"))
        .def("publish", &PlaybackSession::publish)
        .def("poll",
            [](PlaybackSession& self) {
                return std::string(playback_state_name(self.poll()));
            })
        .def("cancel", &PlaybackSession::cancel,
            py::arg("reason") = "cancelled")
        .def("acknowledge_reset", &PlaybackSession::acknowledge_reset)
        .def("finish", &PlaybackSession::finish,
            py::arg("reason") = "finished")
        .def("observe_minitouch_log",
            [](PlaybackSession& self, py::dict event_dict) {
                self.observe_minitouch_log(
                    log_event_from_dict(event_dict));
            },
            py::arg("event"))
        .def("observe_execution", &PlaybackSession::observe_execution,
            py::arg("planned_engine_s"),
            py::arg("actual_engine_s"),
            py::arg("count") = 1)
        .def("state",
            [](const PlaybackSession& self) {
                return std::string(playback_state_name(self.state()));
            })
        .def("report",
            [](const PlaybackSession& self) {
                return playback_report_to_dict(self.report());
            })
        .def("latency_offsets", &PlaybackSession::latency_offsets)
        .def("latency_correction_ms",
            &PlaybackSession::latency_correction_ms,
            py::arg("previous"))
        .def("calibration_event_count",
            &PlaybackSession::calibration_event_count)
        .def("reset_calibration",
            &PlaybackSession::reset_calibration_window);

    py::class_<MinitouchClient>(module, "MinitouchClient")
        .def(py::init<>())
        .def("connect",
            [](MinitouchClient& self, const std::string& host, int port) {
                py::gil_scoped_release release;
                return self.connect(host, port);
            },
            py::arg("host"), py::arg("port"))
        .def("publish",
            [](MinitouchClient& self, const std::string& bytes) {
                py::gil_scoped_release release;
                return self.publish(bytes);
            },
            py::arg("bytes"))
        .def("receive",
            [](MinitouchClient& self, std::size_t max_bytes, int timeout_ms) {
                // recv 可能阻塞到 SO_RCVTIMEO（最长 timeout_ms），必须
                // 释放 GIL，否则后台回读线程会饿死引擎实时循环。
                py::gil_scoped_release release;
                return self.receive(max_bytes, timeout_ms);
            },
            py::arg("max_bytes"), py::arg("timeout_ms") = 500)
        .def("close", &MinitouchClient::close)
        .def_property_readonly("connected",
            [](const MinitouchClient& self) { return self.connected(); });

    module.def("parse_minitouch_log",
        [](const std::string& line) {
            MinitouchLogEvent event;
            if (!parse_minitouch_log(line, &event)) {
                return py::object(py::none());
            }
            py::dict result;
            result["start_ms"] = event.start_ms;
            result["end_ms"] = event.end_ms;
            result["cost_ms"] = event.cost_ms;
            result["command"] = event.command;
            return py::object(result);
        },
        py::arg("line"));

    py::class_<LatencyCalibrator>(module, "LatencyCalibrator")
        .def(py::init<>())
        .def("observe",
            [](LatencyCalibrator& self, py::dict event_dict) {
                MinitouchLogEvent event;
                event.start_ms =
                    event_dict["start_ms"].cast<double>();
                event.end_ms = event_dict["end_ms"].cast<double>();
                event.cost_ms = event_dict["cost_ms"].cast<double>();
                event.command =
                    py::str(event_dict["command"]).cast<std::string>();
                self.observe(event);
            },
            py::arg("event"))
        .def_property_readonly("offsets", &LatencyCalibrator::offsets)
        .def_property_readonly("sample_counts",
            [](const LatencyCalibrator& self) {
                const TouchLatencySampleCounts counts =
                    self.sample_counts();
                py::dict result;
                result["down"] = counts.down;
                result["up"] = counts.up;
                result["move"] = counts.move;
                result["wait"] = counts.wait;
                result["interval"] = counts.interval;
                return result;
            })
        .def("correction_ms", &LatencyCalibrator::correction_ms,
            py::arg("previous"))
        .def("reset", &LatencyCalibrator::reset)
        .def_property_readonly("event_count", &LatencyCalibrator::event_count);

    py::register_exception<ChartParseError>(module, "ChartParseError",
        PyExc_ValueError);
}
