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
