// Pure management kernel. The ABI receives every league dimension and rule.
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <vector>

struct Seat { int player; int origin; int group; };
using Seats = std::vector<Seat>;

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

static std::vector<int> starters(std::vector<int> players, int count,
                                const uint64_t* masks, const uint64_t* slots,
                                const double* priority) {
    std::sort(players.begin(), players.end(), [&](int a, int b) {
        return priority[a] != priority[b] ? priority[a] > priority[b] : a < b;
    });
    std::vector<int> assignment(count, -1), chosen;
    for (int p : players) {
        std::vector<uint8_t> seen(count, false);
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
    int N, D, W, S, T, R, L, I, add_limit, waiver_days, next_day, weekly_lock;
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
};

class Simulation {
    const Input& x;
    std::vector<Team> teams;
    std::vector<uint8_t> free;
    std::vector<int> release, effective;
    int current_day = 0, current_team = 0, current_sample = 0;

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
    }

    void returns(Team& team, int day, const uint8_t* today) {
        auto old_injured = team.injured;
        for (auto seat : old_injured) {
            int p = seat.player;
            // A changed public designation may make an occupied injury slot ineligible.
            if (!today[p] && x.eligible[(day*x.N+p)*x.I+seat.group]) continue;
            auto found = std::find_if(team.injured.begin(), team.injured.end(),
                                     [&](Seat q) { return q.player == p; });
            team.injured.erase(found);
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
            emit(Activate, -1, p);
        }
    }

    void injuries(Team& team, const uint8_t* today) {
        std::vector<int> hurt;
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
                emit(Injury, p, -1);
                break;
            }
        }
    }

    int vacancy(int team, int p) const {
        for (int seat = 0; seat < x.sizes[team]; ++seat) {
            const auto& active = teams[team].active;
            if (std::any_of(active.begin(), active.end(),
                            [&](Seat q) { return q.origin == seat; })) continue;
            if (x.masks[p] & x.masks[x.roster[team * x.R + seat]]) return seat;
        }
        return -1;
    }

    void replacements(int t, int day, const uint8_t* today, const int* ranked) {
        auto& team = teams[t];
        for (int j = 0; j < x.N && int(team.active.size()) < x.sizes[t] &&
                        team.used < x.add_limit; ++j) {
            int p = ranked[j];
            if (!today[p] || !free[p] || release[p] > day) continue;
            int seat = vacancy(t, p);
            if (seat < 0) continue;
            team.active.push_back({p, seat, -1});
            free[p] = false;
            effective[p] = day + x.next_day;
            addition(InjuryAdd);
            emit(InjuryAdd, -1, p);
        }
    }

    bool legal(const Team& team, int old, int added) const {
        if ((x.masks[added] & x.masks[old]) == x.masks[old]) return true;
        std::vector<int> before, after;
        for (auto seat : team.active) {
            before.push_back(seat.player);
            after.push_back(seat.player == old ? added : seat.player);
        }
        return starters(after, x.L, x.masks, x.slots, x.priority).size() >=
               starters(before, x.L, x.masks, x.slots, x.priority).size();
    }

    bool streamable(const Team& team, int seat) const {
        return std::find(team.streamed.begin(), team.streamed.end(), seat) != team.streamed.end() ||
               int(team.streamed.size()) < x.options->flex[current_team];
    }

    struct Swap { int old = -1, added = -1, origin = -1; double gain; };

    Swap choose(const Team& team, const uint8_t* today, bool longer) const {
        const auto& o = *x.options;
        size_t offset = (size_t(current_sample)*x.D+current_day)*x.N;
        const int* order = (longer ? o.long_order : o.short_order) + offset;
        const double* short_value = o.short_values + offset;
        const double* long_value = o.long_values + offset;
        const double* acquired_short = o.acquired_short + offset;
        const double* acquired_long = o.acquired_long + offset;
        Swap best{-1, -1, -1, o.minimum_gain};
        int candidates = 0;
        for (int j = 0; j < x.N && candidates < o.candidates; ++j) {
            int p = order[j];
            if (!today[p] || !free[p] || release[p] > current_day) continue;
            ++candidates;
            for (auto held : team.active) {
                if (!longer && !streamable(team, held.origin)) continue;
                int q = held.player;
                double gain = longer ? acquired_long[p] - long_value[q] :
                    acquired_short[p] - short_value[q] - std::max(0., long_value[q]-acquired_long[p])*o.opportunity_cost;
                if (gain > best.gain && legal(team, q, p)) best = {q, p, held.origin, gain};
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
        free[selected.added] = false;
        effective[selected.added] = current_day + x.next_day;
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
        for (int p : selected) if (today[p]) x.counts[((size_t(sample)*x.T+t)*x.W+x.week[day])*x.N+p] += 1;
        emit(Lineup, -1, -1, selected);
    }

    void validate_team(int t) const {
        const auto& team = teams[t];
        if (int(team.active.size()) > x.sizes[t] || int(team.injured.size()) > x.I ||
            team.used > x.add_limit) throw std::runtime_error("Management capacity violation");
        std::vector<uint8_t> seats(x.R, false);
        for (auto seat : team.active) {
            if (seats[seat.origin]) throw std::runtime_error("Duplicate active seat");
            seats[seat.origin] = true;
        }
    }

    void validate_ownership() const {
        std::vector<uint8_t> held(x.N, false);
        for (const auto& team : teams) for (const auto* group : {&team.active, &team.injured}) {
            for (auto seat : *group) {
                if (held[seat.player] || free[seat.player]) throw std::runtime_error("Management ownership violation");
                held[seat.player] = true;
            }
        }
    }

public:
    explicit Simulation(const Input& input)
        : x(input), teams(x.T), free(x.pool, x.pool+x.N), release(x.N, 0), effective(x.N, 0) {
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
            if (!day || x.period[day] != x.period[day-1]) for (auto& team : teams) {
                team.used = 0; team.injury_adds = 0; team.streamed.clear();
            }
            int first = (day + sample) % x.T;
            for (int turn = 0; turn < x.T; ++turn) {
                int t = (first + turn) % x.T;
                current_team = t;
                returns(teams[t], day, today);
                injuries(teams[t], today);
                replacements(t, day, today, ranked);
                tactical(today);
                score(t, day, sample, today);
                validate_team(t);
            }
            validate_ownership();
        }
    }
};

extern "C" int fba_season(
    int N, int D, int W, int S, int T, int R, int L, int I,
    int add_limit, int waiver_days, int next_day, int weekly_lock,
    const uint8_t* health, const uint8_t* games, const int* week, const int* period,
    const uint64_t* masks, const uint64_t* slots, const double* priority,
    const double* value, const int* order, const uint8_t* eligible, const uint8_t* lock_days,
    const int* roster, const int* sizes, const uint8_t* pool,
    double* counts, const Options* options, char* error, int error_capacity) {
    try {
        if (N < 1 || D < 1 || W < 1 || S < 1 || T < 1 || R < 1 || L < 1 || I < 0)
            throw std::runtime_error("Invalid season dimensions");
        Input input{N, D, W, S, T, R, L, I, add_limit, waiver_days, next_day, weekly_lock,
                    health, games, week, period, masks, slots, priority, value, order,
                    eligible, lock_days, roster, sizes, pool, counts, options};
        for (int sample = 0; sample < S; ++sample) Simulation(input).run(sample);
        return 0;
    } catch (const std::exception& e) {
        if (error_capacity > 0) { std::strncpy(error, e.what(), error_capacity-1); error[error_capacity-1] = 0; }
        return -1;
    }
}
