// Pure management kernel. The ABI receives every league dimension and rule.
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <exception>
#include <numeric>
#include <stdexcept>
#include <unordered_map>
#include <vector>

struct Seat { int player; int origin; int group; };
using Seats = std::vector<Seat>;
struct LegalCheck { uint64_t removed, added; bool allowed; };
struct Candidates {
    int generation = -1;
    int cursor = 0;
    std::vector<int> players;
};

static bool augment(int player, std::vector<uint8_t>& seen,
                    std::vector<int>& assignment, const uint64_t* masks,
                    const uint64_t* slots) {
    for (size_t slot = 0; slot < assignment.size(); ++slot) {
        if (seen[slot] || !(masks[player] & slots[slot])) continue;
        seen[slot] = true;
        if (assignment[slot] < 0 || augment(assignment[slot], seen, assignment, masks, slots)) {
            assignment[slot] = player;
            return true;
        }
    }
    return false;
}

static std::vector<int> starters(std::vector<int>& players, int count,
                                const uint64_t* masks, const uint64_t* slots,
                                const double* priority) {
    if (priority) std::sort(players.begin(), players.end(), [&](int a, int b) {
        return priority[a] != priority[b] ? priority[a] > priority[b] : a < b;
    });
    std::vector<int> assignment(count, -1), chosen;
    chosen.reserve(count);
    std::vector<uint8_t> seen(count);
    for (int p : players) {
        std::fill(seen.begin(), seen.end(), false);
        if (augment(p, seen, assignment, masks, slots)) chosen.push_back(p);
        if (int(chosen.size()) == count) break;
    }
    return chosen;
}

enum Kind { ReturnRelease, ReturnDrop, Activate, Injury, InjuryAdd, Upgrade, Stream, Lineup };
struct Options {
    int reserve, candidates, upgrades, capacity;
    double minimum_gain, opportunity_cost;
    const int *flex, *short_order, *long_order;
    const double *short_values, *long_values, *acquired_short, *acquired_long;
    int *adds, *events, *emitted;
};

struct Input {
    int N, D, W, S, T, R, L, I, add_limit, waiver_days, next_day, weekly_lock, scored_teams;
    const uint8_t *health, *games;
    const int *week, *period;
    const uint64_t *masks, *slots;
    const double *priority, *value;
    const int *order;
    const uint8_t *eligible, *lock_days;
    const int *roster, *sizes;
    const uint8_t *pool;
    double *counts;
    const Options* options;
};

struct Team {
    Seats active, injured;
    std::vector<int> locked;
    int used = 0, injury_adds = 0;
    std::vector<int> streamed;
    std::vector<uint64_t> legal_masks;
    std::vector<LegalCheck> legal_checks;
    int legal_count = -1;
    bool legal_dirty = true;
};

struct MaskHash {
    size_t operator()(const std::vector<uint64_t>& masks) const {
        size_t hash = masks.size();
        for (auto mask : masks) hash ^= size_t(mask) + 0x9e3779b9 + (hash << 6) + (hash >> 2);
        return hash;
    }
};

class MatchingCounts {
    std::unordered_map<std::vector<uint64_t>, int, MaskHash> values;
public:
    int count(std::vector<uint64_t> masks, const Input& input) {
        std::sort(masks.begin(), masks.end());
        auto found = values.find(masks);
        if (found != values.end()) return found->second;
        std::vector<int> players(masks.size());
        std::iota(players.begin(), players.end(), 0);
        // Maximum matching size depends on positions, not player identity or priority.
        int size = int(starters(players, input.L, masks.data(), input.slots, nullptr).size());
        values.emplace(std::move(masks), size);
        return size;
    }
};

class Simulation {
    const Input& x;
    MatchingCounts& matching;
    std::vector<Team> teams;
    std::vector<uint8_t> free;
    std::vector<int> release, effective;
    std::vector<int> hurt;
    std::vector<uint8_t> seat_seen, ownership_seen;
    int current_day = 0, current_team = 0, current_sample = 0;
    int pool_generation = 0;
    Candidates short_candidates, long_candidates, replacement_candidates;
    Seats eligible_buffer;
    std::vector<int> vacant;

    void emit(int kind, int old, int added, const std::vector<int>& started = {}) {
        const auto* o = x.options;
        if (!o || !o->events || current_sample) return;
        if (*o->emitted >= o->capacity) throw std::runtime_error("Season trace capacity exceeded");
        int width = 8 + x.R + x.I + x.L;
        int* event = o->events + width * (*o->emitted)++;
        std::fill(event, event + width, -1);
        event[0] = current_day; event[1] = current_team; event[2] = kind;
        event[3] = old; event[4] = added;
        if (kind != Lineup) return;
        const auto& team = teams[current_team];
        event[5] = int(team.active.size()); event[6] = int(team.injured.size());
        event[7] = int(started.size());
        for (size_t i = 0; i < team.active.size(); ++i) event[8+i] = team.active[i].player;
        for (size_t i = 0; i < team.injured.size(); ++i) event[8+x.R+i] = team.injured[i].player;
        std::copy(started.begin(), started.end(), event + 8+x.R+x.I);
    }

    void addition(int kind) {
        auto& team = teams[current_team];
        ++team.used;
        if (kind == InjuryAdd) ++team.injury_adds;
        if (x.options) ++x.options->adds[((current_sample*x.T+current_team)*x.W+x.week[current_day])*3+kind-InjuryAdd];
    }

    void drop(int p, int day) {
        free[p] = true;
        release[p] = day + x.waiver_days;
        // Waiting players cannot enter today's candidate lists. Daily invalidation
        // below will admit them once their release date is reached.
        if (!x.waiver_days) ++pool_generation;
    }

    void hold(int p) {
        free[p] = false;
        effective[p] = current_day + x.next_day;
        for (auto* cached : {&short_candidates, &long_candidates, &replacement_candidates}) {
            if (cached->generation != pool_generation) continue;
            auto& players = cached->players;
            players.erase(std::remove(players.begin(), players.end(), p), players.end());
        }
    }

    void returns(Team& team, int day, const uint8_t* today) {
        for (size_t i = 0; i < team.injured.size();) {
            auto seat = team.injured[i];
            int p = seat.player;
            // A changed public designation may make an occupied injury slot ineligible.
            if (!today[p] && x.eligible[(day*x.N+p)*x.I+seat.group]) { ++i; continue; }
            team.injured.erase(team.injured.begin() + i);
            auto same = std::find_if(team.active.begin(), team.active.end(),
                                    [&](Seat q) { return q.origin == seat.origin; });
            if (same != team.active.end()) {
                int q = same->player;
                if (x.value[q] > x.value[p] || (x.value[q] == x.value[p] && q < p)) {
                    drop(p, day);
                    emit(ReturnRelease, p, -1);
                    continue;
                }
                drop(q, day);
                team.active.erase(same);
                emit(ReturnDrop, q, -1);
            }
            seat.group = -1;
            team.active.push_back(seat);
            team.legal_dirty = true;
            emit(Activate, -1, p);
        }
    }

    void injuries(Team& team, const uint8_t* today) {
        hurt.clear();
        for (auto seat : team.active) if (!today[seat.player]) hurt.push_back(seat.player);
        std::sort(hurt.begin(), hurt.end(), [&](int p, int q) {
            return x.value[p] != x.value[q] ? x.value[p] > x.value[q] : p < q;
        });
        for (int p : hurt) {
            for (int group = 0; group < x.I; ++group) {
                if (!x.eligible[(current_day*x.N+p)*x.I + group] || std::any_of(
                    team.injured.begin(), team.injured.end(),
                    [&](Seat q) { return q.group == group; })) continue;
                auto held = std::find_if(team.active.begin(), team.active.end(),
                                        [&](Seat q) { return q.player == p; });
                Seat seat = *held;
                seat.group = group;
                team.injured.push_back(seat);
                team.active.erase(held);
                team.legal_dirty = true;
                emit(Injury, p, -1);
                break;
            }
        }
    }

    void vacancies(int t) {
        vacant.clear();
        std::fill(seat_seen.begin(), seat_seen.end(), false);
        for (auto seat : teams[t].active) seat_seen[seat.origin] = true;
        for (int seat = 0; seat < x.sizes[t]; ++seat)
            if (!seat_seen[seat]) vacant.push_back(seat);
    }

    void replacements(int t, const uint8_t* today, const int* ranked) {
        auto& team = teams[t];
        if (int(team.active.size()) >= x.sizes[t] || team.used >= x.add_limit) return;
        vacancies(t);
        size_t j = 0;
        while (int(team.active.size()) < x.sizes[t] && team.used < x.add_limit) {
            auto& players = eligible_candidates(today, ranked, replacement_candidates, int(j)+1);
            if (j >= players.size()) break;
            int p = players[j];
            auto seat = std::find_if(vacant.begin(), vacant.end(), [&](int origin) {
                return x.masks[p] & x.masks[x.roster[t * x.R + origin]];
            });
            if (seat == vacant.end()) { ++j; continue; }
            team.active.push_back({p, *seat, -1});
            vacant.erase(seat);
            team.legal_dirty = true;
            // hold removes p from this list; the next candidate now occupies j.
            hold(p);
            addition(InjuryAdd);
            emit(InjuryAdd, -1, p);
        }
    }

    void prepare_legality(Team& team) const {
        if (!team.legal_dirty) return;
        team.legal_dirty = false;
        std::vector<uint64_t> masks;
        masks.reserve(team.active.size());
        for (auto seat : team.active) masks.push_back(x.masks[seat.player]);
        std::sort(masks.begin(), masks.end());
        if (masks != team.legal_masks) {
            team.legal_masks.swap(masks);
            team.legal_checks.clear();
            team.legal_count = -1;
        }
    }

    bool legal(Team& team, int old, int added) const {
        auto& checks = team.legal_checks;
        int& baseline = team.legal_count;
        if ((x.masks[added] & x.masks[old]) == x.masks[old]) return true;
        auto found = std::find_if(checks.begin(), checks.end(), [&](const LegalCheck& c) {
            return c.removed == x.masks[old] && c.added == x.masks[added];
        });
        if (found != checks.end()) return found->allowed;
        auto after = team.legal_masks;
        if (baseline < 0) baseline = matching.count(after, x);
        *std::find(after.begin(), after.end(), x.masks[old]) = x.masks[added];
        bool allowed = matching.count(std::move(after), x) >= baseline;
        checks.push_back({x.masks[old], x.masks[added], allowed});
        return allowed;
    }

    bool streamable(const Team& team, int seat) const {
        return std::find(team.streamed.begin(), team.streamed.end(), seat) != team.streamed.end() ||
               int(team.streamed.size()) < x.options->flex[current_team];
    }

    const Seats& eligible_seats(const Team& team, bool longer) {
        auto& eligible = eligible_buffer;
        eligible.clear();
        for (auto held : team.active)
            if (longer || streamable(team, held.origin)) eligible.push_back(held);
        return eligible;
    }

    struct Swap { int old = -1, added = -1, origin = -1; double gain; };

    std::vector<int>& eligible_candidates(const uint8_t* today, const int* order,
                                          Candidates& cached, int limit) {
        if (cached.generation != pool_generation) {
            cached.players.clear();
            cached.cursor = 0;
            cached.generation = pool_generation;
        }
        while (cached.cursor < x.N && int(cached.players.size()) < limit) {
            int p = order[cached.cursor++];
            if (!today[p] || !free[p] || release[p] > current_day) continue;
            cached.players.push_back(p);
        }
        return cached.players;
    }

    const std::vector<int>& candidates(const uint8_t* today, bool longer) {
        size_t offset = (size_t(current_sample)*x.D+current_day)*x.N;
        const auto& o = *x.options;
        const int* order = (longer ? o.long_order : o.short_order) + offset;
        auto& cached = longer ? long_candidates : short_candidates;
        return eligible_candidates(today, order, cached, o.candidates);
    }

    Swap choose(Team& team, const uint8_t* today, bool longer) {
        const auto& o = *x.options;
        size_t offset = (size_t(current_sample)*x.D+current_day)*x.N;
        const double* short_value = o.short_values + offset;
        const double* long_value = o.long_values + offset;
        const double* acquired_short = o.acquired_short + offset;
        const double* acquired_long = o.acquired_long + offset;
        Swap best{-1, -1, -1, o.minimum_gain};
        // This choice does not mutate the roster; eligibility and its baseline are invariant.
        const auto& eligible = eligible_seats(team, longer);
        if (eligible.empty()) return best;
        const double* held_values = longer ? long_value : short_value;
        const double* acquired = longer ? acquired_long : acquired_short;
        auto cheapest = std::min_element(eligible.begin(), eligible.end(), [&](Seat a, Seat b) {
            return held_values[a.player] < held_values[b.player];
        });
        double lower = held_values[cheapest->player];
        bool prepared = false;
        const auto& available = candidates(today, longer);
        for (int p : available) {
            // Opportunity cost is nonnegative, so this upper bound cannot discard a better swap.
            if (acquired[p] - lower <= best.gain) continue;
            for (auto held : eligible) {
                int q = held.player;
                double gain = longer ? acquired_long[p] - long_value[q] :
                    acquired_short[p] - short_value[q] - std::max(0., long_value[q]-acquired_long[p])*o.opportunity_cost;
                if (gain <= best.gain) continue;
                if (!prepared) { prepare_legality(team); prepared = true; }
                if (legal(team, q, p)) best = {q, p, held.origin, gain};
            }
        }
        return best;
    }

    void tactical(const uint8_t* today) {
        const auto* o = x.options;
        if (!o || !o->flex) return;
        auto& team = teams[current_team];
        int limit = x.add_limit - std::max(0, o->reserve - team.injury_adds);
        if (team.active.empty() || team.used >= limit) return;
        bool new_week = !current_day || x.week[current_day] != x.week[current_day-1];
        Swap selected{-1, -1, -1, o->minimum_gain};
        int kind = Upgrade;
        if (o->upgrades && new_week) selected = choose(team, today, true);
        if (selected.old < 0 && o->flex[current_team]) { selected = choose(team, today, false); kind = Stream; }
        if (selected.old < 0) return;
        auto held = std::find_if(team.active.begin(), team.active.end(),
            [&](Seat s) { return s.player == selected.old; });
        team.active.erase(held);
        drop(selected.old, current_day);
        emit(kind, selected.old, selected.added);
        team.active.push_back({selected.added, selected.origin, -1});
        team.legal_dirty = true;
        hold(selected.added);
        if (kind == Stream && std::find(team.streamed.begin(), team.streamed.end(), selected.origin) == team.streamed.end())
            team.streamed.push_back(selected.origin);
        addition(kind);
    }

    void score(int t, int day, int sample, const uint8_t* today) {
        auto& team = teams[t];
        std::vector<int> playing;
        if (x.weekly_lock && x.lock_days[day]) {
            for (auto seat : team.active) {
                if (effective[seat.player] <= day && today[seat.player])
                    playing.push_back(seat.player);
            }
            team.locked = starters(playing, x.L, x.masks, x.slots, x.priority);
            playing.clear();
        }
        for (auto seat : team.active) {
            int p = seat.player;
            bool locked = !x.weekly_lock || std::find(team.locked.begin(), team.locked.end(), p) != team.locked.end();
            if (x.games[day*x.N+p] && (x.weekly_lock || today[p]) && effective[p] <= day && locked) playing.push_back(p);
        }
        auto selected = starters(playing, x.L, x.masks, x.slots, x.priority);
        // A weekly selection stays locked even when today's report predicts a DNP.
        // Simulated counts use health; historical replay scores the selection with actual boxes.
        for (int p : selected) if (today[p]) x.counts[((size_t(sample)*x.scored_teams+t)*x.W+x.week[day])*x.N+p] += 1;
        emit(Lineup, -1, -1, selected);
    }

    void validate_team(int t) {
        const auto& team = teams[t];
        if (int(team.active.size()) > x.sizes[t] || int(team.injured.size()) > x.I ||
            team.used > x.add_limit) throw std::runtime_error("Management capacity violation");
        std::fill(seat_seen.begin(), seat_seen.end(), false);
        for (auto seat : team.active) {
            if (seat_seen[seat.origin]) throw std::runtime_error("Duplicate active seat");
            seat_seen[seat.origin] = true;
        }
    }

    void validate_ownership() {
        std::fill(ownership_seen.begin(), ownership_seen.end(), false);
        for (const auto& team : teams) for (const auto* group : {&team.active, &team.injured}) {
            for (auto seat : *group) {
                if (ownership_seen[seat.player] || free[seat.player]) throw std::runtime_error("Management ownership violation");
                ownership_seen[seat.player] = true;
            }
        }
    }

public:
    Simulation(const Input& input, MatchingCounts& counts)
        : x(input), matching(counts), teams(x.T), free(x.pool, x.pool+x.N), release(x.N, 0), effective(x.N, 0),
          seat_seen(x.R), ownership_seen(x.N) {
        hurt.reserve(x.R);
        eligible_buffer.reserve(x.R);
        vacant.reserve(x.R);
        for (int t = 0; t < x.T; ++t) {
            if (x.sizes[t] < 0 || x.sizes[t] > x.R) throw std::runtime_error("Invalid roster size");
            for (int seat = 0; seat < x.sizes[t]; ++seat) {
                int p = x.roster[t*x.R+seat];
                if (p < 0 || p >= x.N) throw std::runtime_error("Invalid roster player");
                teams[t].active.push_back({p, seat, -1});
                free[p] = false;
            }
        }
    }

    void run(int sample) {
        for (int day = 0; day < x.D; ++day) {
            const auto* today = x.health + (size_t(sample)*x.D+day)*x.N;
            const auto* ranked = x.order + (size_t(sample)*x.D+day)*x.N;
            current_day = day; current_sample = sample;
            ++pool_generation; // Public health, waiver eligibility and rankings can change daily.
            if (!day || x.period[day] != x.period[day-1]) for (auto& team : teams) {
                team.used = 0; team.injury_adds = 0; team.streamed.clear();
            }
            int first = (day + sample) % x.T;
            for (int turn = 0; turn < x.T; ++turn) {
                int t = (first + turn) % x.T;
                current_team = t;
                returns(teams[t], day, today);
                injuries(teams[t], today);
                replacements(t, today, ranked);
                tactical(today);
                if (t < x.scored_teams) score(t, day, sample, today);
                validate_team(t);
            }
            validate_ownership();
        }
    }
};

extern "C" int fba_season(
    int N, int D, int W, int S, int T, int R, int L, int I,
    int add_limit, int waiver_days, int next_day, int weekly_lock, int scored_teams,
    const uint8_t* health, const uint8_t* games, const int* week, const int* period,
    const uint64_t* masks, const uint64_t* slots, const double* priority,
    const double* value, const int* order, const uint8_t* eligible, const uint8_t* lock_days,
    const int* roster, const int* sizes, const uint8_t* pool,
    double* counts, const Options* options, char* error, int error_capacity) {
    try {
        if (N < 1 || D < 1 || W < 1 || S < 1 || T < 1 || R < 1 || L < 1 || I < 0 || scored_teams < 1 || scored_teams > T)
            throw std::runtime_error("Invalid season dimensions");
        Input input{N, D, W, S, T, R, L, I, add_limit, waiver_days, next_day, weekly_lock, scored_teams,
                    health, games, week, period, masks, slots, priority, value, order,
                    eligible, lock_days, roster, sizes, pool, counts, options};
        MatchingCounts matching;
        for (int sample = 0; sample < S; ++sample) Simulation(input, matching).run(sample);
        return 0;
    } catch (const std::exception& e) {
        if (error_capacity > 0) { std::strncpy(error, e.what(), error_capacity-1); error[error_capacity-1] = 0; }
        return -1;
    }
}
