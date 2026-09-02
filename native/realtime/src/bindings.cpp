#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <sstream>
#include <string>

#include "maabangdream/chart_timeline.hpp"
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
        .def(py::init([](int down_ms, int up_ms, int move_ms, int tap_ms,
                         int flick_ms) {
            TouchLatencyOffsets offsets;
            offsets.down_ms = down_ms;
            offsets.up_ms = up_ms;
            offsets.move_ms = move_ms;
            offsets.tap_ms = tap_ms;
            offsets.flick_ms = flick_ms;
            return offsets;
        }),
        py::arg("down_ms") = 0,
        py::arg("up_ms") = 0,
        py::arg("move_ms") = 0,
        py::arg("tap_ms") = 0,
        py::arg("flick_ms") = 0)
        .def_readwrite("down_ms", &TouchLatencyOffsets::down_ms)
        .def_readwrite("up_ms", &TouchLatencyOffsets::up_ms)
        .def_readwrite("move_ms", &TouchLatencyOffsets::move_ms)
        .def_readwrite("tap_ms", &TouchLatencyOffsets::tap_ms)
        .def_readwrite("flick_ms", &TouchLatencyOffsets::flick_ms);

    py::class_<TouchScriptCompiler>(module, "TouchScriptCompiler")
        .def(py::init([](const TouchLatencyOffsets& offsets) {
            return std::make_unique<TouchScriptCompiler>(offsets);
        }), py::arg("offsets") = TouchLatencyOffsets{})
        .def("compile",
            [](const TouchScriptCompiler& self, py::list actions,
               py::dict config_dict, double start_engine_time) {
                std::vector<ScheduledAction> parsed;
                parsed.reserve(actions.size());
                for (const auto handle : actions) {
                    parsed.push_back(action_from_dict(
                        handle.cast<py::dict>()));
                }
                const std::vector<std::string> lines = self.compile(
                    std::move(parsed),
                    config_from_dict(config_dict),
                    start_engine_time);
                py::list result;
                for (const std::string& text : lines) {
                    result.append(text);
                }
                return result;
            },
            py::arg("actions"),
            py::arg("config") = py::dict(),
            py::arg("start_engine_time") = 0.0);

    py::register_exception<ChartParseError>(module, "ChartParseError",
        PyExc_ValueError);
}
