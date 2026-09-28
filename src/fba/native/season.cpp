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
};

struct Team {
    Seats active, injured;
    std::vector<int> locked;
    int used = 0;
};

class Simulation {
    const Input& x;
    std::vector<Team> teams;
    std::vector<uint8_t> free;
    std::vector<int> release, effective;

    void drop(int p, int day) {
        free[p] = true;
        release[p] = day + x.waiver_days;
    }

    void returns(Team& team, int day, const uint8_t* today) {
        auto old_injured = team.injured;
        for (auto seat : old_injured) {
            int p = seat.player;
            if (!today[p]) continue;
            auto found = std::find_if(team.injured.begin(), team.injured.end(),
                                     [&](Seat q) { return q.player == p; });
            team.injured.erase(found);
            auto same = std::find_if(team.active.begin(), team.active.end(),
                                    [&](Seat q) { return q.origin == seat.origin; });
            if (same != team.active.end()) {
                int q = same->player;
                if (x.value[q] > x.value[p] || (x.value[q] == x.value[p] && q < p)) {
                    drop(p, day);
                    continue;
                }
                drop(q, day);
                team.active.erase(same);
            }
            seat.group = -1;
            team.active.push_back(seat);
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
                if (!x.eligible[p * x.I + group] || std::any_of(
                    team.injured.begin(), team.injured.end(),
                    [&](Seat q) { return q.group == group; })) continue;
                auto held = std::find_if(team.active.begin(), team.active.end(),
                                        [&](Seat q) { return q.player == p; });
                Seat seat = *held;
                seat.group = group;
                team.injured.push_back(seat);
                team.active.erase(held);
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
            ++team.used;
        }
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
            if (x.games[day*x.N+p] && today[p] && effective[p] <= day && locked) playing.push_back(p);
        }
        for (int p : starters(playing, x.L, x.masks, x.slots, x.priority))
            x.counts[((size_t(sample)*x.T+t)*x.W+x.week[day])*x.N+p] += 1;
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
            if (!day || x.period[day] != x.period[day-1]) for (auto& team : teams) team.used = 0;
            int first = (day + sample) % x.T;
            for (int turn = 0; turn < x.T; ++turn) {
                int t = (first + turn) % x.T;
                returns(teams[t], day, today);
                injuries(teams[t], today);
                replacements(t, day, today, ranked);
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
    double* counts, char* error, int error_capacity) {
    try {
        if (N < 1 || D < 1 || W < 1 || S < 1 || T < 1 || R < 1 || L < 1 || I < 0)
            throw std::runtime_error("Invalid season dimensions");
        Input input{N, D, W, S, T, R, L, I, add_limit, waiver_days, next_day, weekly_lock,
                    health, games, week, period, masks, slots, priority, value, order,
                    eligible, lock_days, roster, sizes, pool, counts};
        for (int sample = 0; sample < S; ++sample) Simulation(input).run(sample);
        return 0;
    } catch (const std::exception& e) {
        if (error_capacity > 0) { std::strncpy(error, e.what(), error_capacity-1); error[error_capacity-1] = 0; }
        return -1;
    }
}
