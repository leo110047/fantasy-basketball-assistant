// Test-only access to internal invariant guards; production ABI stays unchanged.
#include <functional>
#include <string>
#include "../src/fba/native/season.cpp"

static void reject(const Input& input, const std::function<void(Simulation&)>& corrupt,
                   const char* expected) {
    MatchingCounts matching;
    Simulation simulation(input, matching);
    simulation.validate_team(0);
    simulation.validate_ownership();
    corrupt(simulation);
    try {
        simulation.validate_team(0);
        simulation.validate_ownership();
    } catch (const std::runtime_error& error) {
        if (std::string(error.what()) != expected) throw;
        return;
    }
    throw std::runtime_error("Corrupt management state was accepted");
}

static void candidates_follow_public_eligibility(Input input) {
    MatchingCounts matching;
    input.waiver_days = 2;
    Simulation simulation(input, matching);
    uint8_t today[] = {1, 1, 1, 1};
    const int order[] = {3, 2, 1, 0};
    auto check = [&](std::vector<int> expected) {
        for (auto* cache : {&simulation.short_candidates, &simulation.long_candidates,
                            &simulation.replacement_candidates}) {
            if (simulation.eligible_candidates(today, order, *cache, input.N) != expected)
                throw std::runtime_error("Candidate eligibility differs from public state");
        }
    };
    check({3, 2});
    simulation.hold(3);
    check({2});
    simulation.drop(3, 0);
    check({2});
    simulation.current_day = 1; ++simulation.pool_generation;
    check({2});
    simulation.current_day = 2; ++simulation.pool_generation;
    check({3, 2});
    // Public health changes invalidate all three rankings on the next day.
    today[3] = 0;
    simulation.current_day = 3; ++simulation.pool_generation;
    check({2});
    input.waiver_days = 0;
    simulation.teams[0].active.clear();
    simulation.drop(0, 3);
    check({2, 0});
    simulation.hold(2);
    check({0});
}

static void candidate_prefix_extends_after_acquisition(Input input) {
    MatchingCounts matching;
    Simulation simulation(input, matching);
    const uint8_t today[] = {1, 1, 1, 1};
    const int order[] = {3, 2, 1, 0};
    for (auto* cache : {&simulation.short_candidates, &simulation.long_candidates,
                        &simulation.replacement_candidates}) {
        if (simulation.eligible_candidates(today, order, *cache, 1) != std::vector<int>{3})
            throw std::runtime_error("Candidate prefix did not preserve rank order");
    }
    simulation.hold(3);
    for (auto* cache : {&simulation.short_candidates, &simulation.long_candidates,
                        &simulation.replacement_candidates}) {
        if (simulation.eligible_candidates(today, order, *cache, 1) != std::vector<int>{2})
            throw std::runtime_error("Acquisition did not extend the candidate prefix");
    }
    simulation.hold(2);
    if (!simulation.eligible_candidates(today, order, simulation.short_candidates, input.N).empty())
        throw std::runtime_error("Exhausted pool admitted a held player");
}

int main() {
    const uint8_t pool[] = {0, 0, 1, 1};
    const int roster[] = {0, -1, 1, -1};
    int sizes[] = {1, 1};
    Input input{};
    input.N = 4;
    input.T = 2;
    input.R = 2;
    input.I = 1;
    input.add_limit = 1;
    input.pool = pool;
    input.roster = roster;
    input.sizes = sizes;
    candidates_follow_public_eligibility(input);
    candidate_prefix_extends_after_acquisition(input);
    reject(input, [](Simulation& s) { s.teams[0].active.push_back({2, 1, -1}); },
           "Management capacity violation");
    reject(input, [](Simulation& s) { s.teams[0].injured = {{2, 0, 0}, {3, 1, 0}}; },
           "Management capacity violation");
    reject(input, [](Simulation& s) { s.teams[0].used = 2; },
           "Management capacity violation");
    reject(input, [&](Simulation& s) {
        sizes[0] = 2;
        s.teams[0].active.push_back({2, 0, -1});
    }, "Duplicate active seat");
    sizes[0] = 1;
    reject(input, [](Simulation& s) { s.teams[1].active[0].player = 0; },
           "Management ownership violation");
    reject(input, [](Simulation& s) { s.free[0] = true; },
           "Management ownership violation");
}
