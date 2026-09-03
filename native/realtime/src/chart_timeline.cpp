#include "maabangdream/chart_timeline.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "nlohmann/json.hpp"

namespace mbdr {

using nlohmann::json;

namespace {

// 与 Python 端一致：可见 lane 必须是 0..6 的整数，布尔值显式拒绝。
int parse_lane(const json& value, const char* label) {
    if (value.is_boolean()) {
        throw ChartParseError(std::string(label) + " must be an integer from 0 to 6");
    }
    if (value.is_number_float()) {
        const double raw = value.get<double>();
        if (raw != std::floor(raw)) {
            throw ChartParseError(std::string(label) + " must be an integer from 0 to 6");
        }
    }
    if (!value.is_number_integer() && !value.is_number_unsigned() &&
        !value.is_number_float()) {
        throw ChartParseError(std::string(label) + " must be an integer from 0 to 6");
    }
    const int lane = value.get<int>();
    if (lane < 0 || lane >= static_cast<int>(kLaneCount)) {
        throw ChartParseError(std::string(label) + " must be an integer from 0 to 6");
    }
    return lane;
}

double parse_connection_lane(const json& value, bool hidden) {
    if (value.is_boolean()) {
        throw ChartParseError("connection lane must be numeric");
    }
    if (!value.is_number()) {
        throw ChartParseError("connection lane must be numeric");
    }
    const double lane = value.get<double>();
    if (hidden) {
        if (lane < -0.5 || lane > 6.5) {
            throw ChartParseError("hidden connection lane must be within -0.5..6.5");
        }
        return lane;
    }
    return static_cast<double>(parse_lane(value, "connection lane"));
}

double parse_finite(const json& value, const char* label) {
    if (value.is_boolean() || !value.is_number()) {
        throw ChartParseError(std::string(label) + " must be numeric");
    }
    const double result = value.get<double>();
    if (!std::isfinite(result)) {
        throw ChartParseError(std::string(label) + " must be finite");
    }
    return result;
}

int parse_direction(const json& value) {
    if (value.is_null()) {
        return 0;
    }
    if (!value.is_string()) {
        throw ChartParseError("flick direction must be a string");
    }
    const std::string direction = value.get<std::string>();
    if (direction == "Left") {
        return -1;
    }
    if (direction == "Right") {
        return 1;
    }
    throw ChartParseError(
        "unsupported directional flick: '" + direction + "'");
}

// 安全读取可缺省字段。注意：对 const json 使用 operator[] 访问缺失键在
// NDEBUG 构建下是未定义行为（断言被移除后解引用 end()），必须用 find。
const json* find_key(const json& object, const char* key) {
    if (!object.is_object()) {
        return nullptr;
    }
    const auto found = object.find(key);
    return found == object.end() ? nullptr : &*found;
}

int direction_of(const json& note) {
    const json* direction = find_key(note, "direction");
    if (direction == nullptr || direction->is_null()) {
        return 0;
    }
    return parse_direction(*direction);
}

// 读取文件并剥离 UTF-8 BOM（与 Python utf-8-sig 一致）。
std::string read_file_stripped_bom(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        throw ChartParseError("cannot read chart file: " + path);
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    std::string text = buffer.str();
    if (text.size() >= 3 &&
        static_cast<unsigned char>(text[0]) == 0xEF &&
        static_cast<unsigned char>(text[1]) == 0xBB &&
        static_cast<unsigned char>(text[2]) == 0xBF) {
        text.erase(0, 3);
    }
    return text;
}

// 解包 schema-v1 包装或旧版裸数组，返回 raw 谱面列表。
const json& unwrap_chart(const json& payload) {
    if (payload.is_array()) {
        return payload;
    }
    if (!payload.is_object()) {
        throw ChartParseError("chart JSON must be a list or schema-v1 object");
    }
    if (payload.value("schema_version", -1) != 1) {
        throw ChartParseError(
            "unsupported chart schema: '" +
            payload.value("schema_version", json()).dump() + "'");
    }
    const json* raw_pointer = find_key(payload, "chart");
    if (raw_pointer == nullptr) {
        throw ChartParseError("schema-v1 chart field must be a list");
    }
    const auto& raw = *raw_pointer;
    if (!raw.is_array()) {
        throw ChartParseError("schema-v1 chart field must be a list");
    }
    return raw;
}

// 与 Python _parse_hold_path 一致：按 beat 排序，丢弃隐藏首尾，保留内部
// 隐藏连接点用于几何。全部连接点都隐藏或只有一个可见点则返回 nullopt。
struct ParsedPath {
    bool valid = false;
    bool single_point = false;
    HoldPath path;
};

ParsedPath parse_hold_path(
    const json& note,
    int note_index,
    const TempoMap& tempo_map,
    const std::string& note_type) {
    ParsedPath result;
    const json* raw_connections_pointer = find_key(note, "connections");
    if (raw_connections_pointer == nullptr ||
        !raw_connections_pointer->is_array()) {
        throw ChartParseError("Long/Slide connections must be a list");
    }
    const auto& raw_connections = *raw_connections_pointer;
    std::vector<json> ordered(raw_connections.begin(), raw_connections.end());
    std::sort(ordered.begin(), ordered.end(), [](const json& lhs, const json& rhs) {
        return lhs.at("beat").get<double>() < rhs.at("beat").get<double>();
    });
    std::vector<int> visible;
    for (std::size_t index = 0; index < ordered.size(); ++index) {
        if (!ordered[index].value("hidden", false)) {
            visible.push_back(static_cast<int>(index));
        }
    }
    if (visible.empty()) {
        return result;
    }
    const int first = visible.front();
    const int last = visible.back();
    result.valid = true;
    result.path.note_index = note_index;
    result.path.note_type = note_type;
    for (int index = first; index <= last; ++index) {
        const auto& item = ordered[static_cast<std::size_t>(index)];
        const bool hidden = item.value("hidden", false);
        const int direction = direction_of(item);
        const bool flick =
            item.value("flick", false) || direction != 0;
        PathPoint point;
        point.time_s = tempo_map.seconds_at(parse_finite(item.at("beat"), "connection beat"));
        point.lane = parse_connection_lane(item.at("lane"), hidden);
        point.hidden = hidden;
        point.flick = flick;
        point.direction = static_cast<int8_t>(direction);
        result.path.points.push_back(point);
    }
    result.single_point = result.path.points.size() == 1;
    return result;
}

}  // namespace

TempoMap::TempoMap(std::vector<TempoChange> changes) : changes_(std::move(changes)) {
    if (changes_.empty()) {
        throw ChartParseError("chart must contain at least one BPM event");
    }
    beats_.clear();
    beats_.reserve(changes_.size());
    for (const auto& change : changes_) {
        beats_.push_back(change.beat);
    }
}

TempoMap TempoMap::from_events(
    const std::vector<std::pair<double, double>>& beat_bpm) {
    // 同一 beat 重复出现时取最后写入值（与 Python dict 覆盖语义一致）。
    std::vector<std::pair<double, double>> deduped;
    for (const auto& entry : beat_bpm) {
        const double beat = entry.first;
        const double bpm = entry.second;
        auto found = std::find_if(deduped.begin(), deduped.end(),
            [beat](const auto& item) { return item.first == beat; });
        if (found != deduped.end()) {
            found->second = bpm;
        } else {
            deduped.emplace_back(beat, bpm);
        }
    }
    std::sort(deduped.begin(), deduped.end());
    if (deduped.empty()) {
        throw ChartParseError("chart must contain at least one BPM event");
    }
    if (deduped.front().first > 0.0) {
        deduped.insert(deduped.begin(), {0.0, deduped.front().second});
    }
    std::vector<TempoChange> changes;
    double time_s = 0.0;
    double previous_beat = deduped.front().first;
    double previous_bpm = deduped.front().second;
    changes.push_back(TempoChange{previous_beat, previous_bpm, 0.0});
    for (std::size_t index = 1; index < deduped.size(); ++index) {
        const double beat = deduped[index].first;
        const double bpm = deduped[index].second;
        time_s += (beat - previous_beat) * 60.0 / previous_bpm;
        changes.push_back(TempoChange{beat, bpm, time_s});
        previous_beat = beat;
        previous_bpm = bpm;
    }
    return TempoMap(std::move(changes));
}

double TempoMap::seconds_at(double beat) const {
    if (!std::isfinite(beat)) {
        throw ChartParseError("note beat must be finite");
    }
    const auto upper = std::upper_bound(beats_.begin(), beats_.end(), beat);
    std::size_t index = upper == beats_.begin()
        ? 0
        : static_cast<std::size_t>(upper - beats_.begin()) - 1;
    const TempoChange& change = changes_[index];
    return change.time_s + (beat - change.beat) * 60.0 / change.bpm;
}

ChartTimeline ChartTimeline::from_json_file(const std::string& path) {
    return from_json_string(read_file_stripped_bom(path));
}

ChartTimeline ChartTimeline::from_json_string(const std::string& text) {
    json payload;
    try {
        payload = json::parse(text);
    } catch (const json::exception& exc) {
        throw ChartParseError(std::string("invalid chart JSON: ") + exc.what());
    }
    const json& raw = unwrap_chart(payload);

    // BPM 事件。
    std::vector<std::pair<double, double>> bpm_events;
    for (const auto& item : raw) {
        if (!item.is_object()) {
            throw ChartParseError("chart entries must be JSON objects");
        }
        if (item.value("type", "") == "BPM") {
            const double beat = parse_finite(item.at("beat"), "BPM beat");
            const double bpm = parse_finite(item.at("bpm"), "BPM value");
            if (bpm <= 0.0) {
                throw ChartParseError("BPM must be positive");
            }
            bpm_events.emplace_back(beat, bpm);
        }
    }
    TempoMap tempo_map = TempoMap::from_events(bpm_events);

    ChartTimeline timeline;
    timeline.tempo_map = std::move(tempo_map);
    if (payload.is_object()) {
        if (payload.contains("song") && payload["song"].is_object()) {
            timeline.bestdori_song_id =
                payload["song"].value("bestdori_id", -1);
        }
        if (payload.contains("difficulty") && payload["difficulty"].is_object()) {
            timeline.difficulty =
                payload["difficulty"].value("name", std::string{});
            timeline.level = payload["difficulty"].value("level", -1);
        }
    }

    int note_index = 0;
    for (const auto& note : raw) {
        if (!note.is_object()) {
            throw ChartParseError("chart entries must be JSON objects");
        }
        const std::string type = note.value("type", "");
        if (type == "BPM" || type == "System") {
            continue;
        }
        if (type == "Single") {
            ChartJudgement judgement;
            judgement.time_s =
                timeline.tempo_map.seconds_at(
                    parse_finite(note.at("beat"), "note beat"));
            judgement.lane = static_cast<uint8_t>(
                parse_lane(note.at("lane"), "lane"));
            judgement.kind = JudgementKind::Tap;
            judgement.note_index = note_index++;
            judgement.flick = note.value("flick", false);
            judgement.direction = static_cast<int8_t>(direction_of(note));
            timeline.judgements.push_back(judgement);
            continue;
        }
        if (type == "Directional") {
            const int direction = direction_of(note);
            if (direction == 0) {
                throw ChartParseError("unsupported directional flick direction");
            }
            ChartJudgement judgement;
            judgement.time_s =
                timeline.tempo_map.seconds_at(
                    parse_finite(note.at("beat"), "note beat"));
            judgement.lane = static_cast<uint8_t>(
                parse_lane(note.at("lane"), "lane"));
            judgement.kind = JudgementKind::Tap;
            judgement.note_index = note_index++;
            judgement.flick = true;
            judgement.direction = static_cast<int8_t>(direction);
            timeline.judgements.push_back(judgement);
            continue;
        }
        if (type == "Long" || type == "Slide") {
            ParsedPath parsed =
                parse_hold_path(note, note_index, timeline.tempo_map, type);
            if (!parsed.valid) {
                continue;
            }
            if (parsed.single_point) {
                // Bestdori 会把单连接点 Long/Slide 修复成普通判定；零时长
                // hold 会在同一帧产生 DOWN+UP，破坏触点状态。
                const PathPoint& point = parsed.path.points.front();
                ChartJudgement judgement;
                judgement.time_s = point.time_s;
                judgement.lane = static_cast<uint8_t>(point.lane);
                judgement.kind = JudgementKind::Tap;
                judgement.note_index = note_index++;
                judgement.flick = point.flick;
                judgement.direction = static_cast<int8_t>(point.direction);
                timeline.judgements.push_back(judgement);
                continue;
            }
            const PathPoint& head = parsed.path.points.front();
            const PathPoint& tail = parsed.path.points.back();
            const bool tail_flick = tail.flick;
            ChartJudgement head_judgement;
            head_judgement.time_s = head.time_s;
            head_judgement.lane = static_cast<uint8_t>(head.lane);
            head_judgement.kind = JudgementKind::HoldHead;
            head_judgement.note_index = note_index;
            head_judgement.tail_flick = tail_flick;
            ChartJudgement tail_judgement;
            tail_judgement.time_s = tail.time_s;
            tail_judgement.lane = static_cast<uint8_t>(tail.lane);
            tail_judgement.kind = JudgementKind::HoldTail;
            tail_judgement.note_index = note_index;
            tail_judgement.flick = tail_flick;
            tail_judgement.direction = static_cast<int8_t>(tail.direction);
            tail_judgement.tail_flick = tail_flick;
            timeline.judgements.push_back(head_judgement);
            timeline.judgements.push_back(tail_judgement);
            timeline.hold_paths.push_back(parsed.path);
            ++note_index;
            continue;
        }
        throw ChartParseError("unsupported chart note type: '" + type + "'");
    }

    std::sort(timeline.judgements.begin(), timeline.judgements.end(),
        [](const ChartJudgement& lhs, const ChartJudgement& rhs) {
            if (lhs.time_s != rhs.time_s) {
                return lhs.time_s < rhs.time_s;
            }
            if (lhs.lane != rhs.lane) {
                return lhs.lane < rhs.lane;
            }
            return lhs.note_index < rhs.note_index;
        });
    timeline.start_time_s =
        timeline.judgements.empty() ? 0.0 : timeline.judgements.front().time_s;
    timeline.end_time_s =
        timeline.judgements.empty() ? 0.0 : timeline.judgements.back().time_s;
    return timeline;
}

const char* note_kind_name(NoteKind kind) noexcept {
    switch (kind) {
        case NoteKind::Tap: return "tap";
        case NoteKind::Flick: return "flick";
        case NoteKind::Skill: return "skill";
        case NoteKind::Hold: return "hold";
    }
    return "unknown";
}

const char* judgement_kind_name(JudgementKind kind) noexcept {
    switch (kind) {
        case JudgementKind::Tap: return "tap";
        case JudgementKind::HoldHead: return "hold-head";
        case JudgementKind::HoldTail: return "hold-tail";
    }
    return "unknown";
}

const char* action_kind_name(ActionKind kind) noexcept {
    switch (kind) {
        case ActionKind::Tap: return "tap";
        case ActionKind::Flick: return "flick";
        case ActionKind::Down: return "down";
        case ActionKind::Move: return "move";
        case ActionKind::Up: return "up";
    }
    return "unknown";
}

}  // namespace mbdr
