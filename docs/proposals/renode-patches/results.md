# Raw results (median of n, real-time factor; host: Apple M-series via Colima, 4 cores)

## Final A/B: stock vs P2+P3+P5 (c235) vs the release package, n=5

| system | path | phase | stock | c235 | release |
|---|---|---|---|---|---|
| ams | bench | boot | 1.33x n=5 | 1.54x (+15%) n=5 | 1.31x (-2%) n=5 |
| ams | bench | steady | 2.09x n=5 | 2.69x (+28%) n=5 | 1.99x (-5%) n=5 |
| ams | sim | boot | 0.91x n=5 | 1.31x (+44%) n=5 | 1.07x (+18%) n=5 |
| ams | sim | steady | 1.94x n=5 | 3.06x (+58%) n=5 | 2.23x (+15%) n=5 |
| ecu | bench | boot | 1.34x n=5 | 1.53x (+14%) n=5 | 1.15x (-14%) n=5 |
| ecu | bench | steady | 1.69x n=5 | 2.29x (+36%) n=5 | 1.60x (-5%) n=5 |
| ecu | sim | boot | 1.34x n=5 | 1.54x (+15%) n=5 | 1.35x (+1%) n=5 |
| ecu | sim | steady | 2.73x n=5 | 3.85x (+41%) n=5 | 2.70x (-1%) n=5 |
| ecu-ams | bench | boot | 0.81x n=5 | 1.11x (+38%) n=5 | 0.96x (+20%) n=5 |
| ecu-ams | bench | steady | 0.88x n=5 | 1.28x (+47%) n=5 | 1.03x (+18%) n=5 |
| ecu-ams | sim | boot | 0.78x n=5 | 0.75x (-4%) n=5 | 0.83x (+6%) n=5 |
| ecu-ams | sim | steady | 1.08x n=5 | 1.23x (+13%) n=5 | 1.20x (+11%) n=5 |

## Per patch, n=3 (combined-with-P1 not shown)

| system | path | phase | stock | p1-nospin | p2-clock | p3-mmio | p5-alloc |
|---|---|---|---|---|---|---|---|
| ams | bench | boot | 1.21x n=3 | 1.18x (-3%) n=3 | 1.19x (-1%) n=3 | 1.31x (+8%) n=3 | 1.27x (+4%) n=3 |
| ams | bench | steady | 1.88x n=3 | 1.73x (-8%) n=3 | 2.01x (+7%) n=3 | 1.98x (+6%) n=3 | 1.95x (+4%) n=3 |
| ams | sim | boot | 0.80x n=3 | 0.94x (+18%) n=3 | 0.94x (+18%) n=3 | 1.11x (+39%) n=3 | 0.99x (+24%) n=3 |
| ams | sim | steady | 1.66x n=3 | 1.79x (+8%) n=3 | 2.03x (+22%) n=3 | 2.50x (+51%) n=3 | 2.07x (+25%) n=3 |
| ecu | bench | boot | 1.05x n=3 | 1.22x (+17%) n=3 | 1.30x (+25%) n=3 | 1.29x (+24%) n=3 | 1.28x (+22%) n=3 |
| ecu | bench | steady | 1.42x n=3 | 1.56x (+10%) n=3 | 2.02x (+42%) n=3 | 1.67x (+18%) n=3 | 1.67x (+18%) n=3 |
| ecu | sim | boot | 1.25x n=3 | 1.15x (-8%) n=3 | 1.26x (+0%) n=3 | 1.29x (+3%) n=3 | 1.33x (+6%) n=3 |
| ecu | sim | steady | 2.47x n=3 | 2.07x (-16%) n=3 | 3.01x (+22%) n=3 | 2.60x (+5%) n=3 | 2.79x (+13%) n=3 |
| ecu-ams | bench | boot | 0.85x n=3 | 0.82x (-4%) n=3 | 0.92x (+7%) n=3 | 0.91x (+7%) n=3 | 0.88x (+4%) n=3 |
| ecu-ams | bench | steady | 0.89x n=3 | 0.82x (-8%) n=3 | 0.99x (+12%) n=3 | 0.93x (+4%) n=3 | 0.93x (+5%) n=3 |
| ecu-ams | sim | boot | 0.66x n=3 | 0.66x (-1%) n=3 | 0.76x (+15%) n=3 | 0.70x (+7%) n=3 | 0.75x (+14%) n=3 |
| ecu-ams | sim | steady | 1.00x n=3 | 0.90x (-11%) n=3 | 1.06x (+6%) n=3 | 1.05x (+5%) n=3 | 1.14x (+14%) n=3 |

## Quantum (measurement only, P2+P3+P5 build, ecu-ams): 500 us vs 2 ms, n=3

| path | phase | 500 us | 2 ms |
|---|---|---|---|
| bench | boot | 1.15x | 1.60x |
| bench | steady | 1.34x | 1.91x |
| sim | boot | 0.85x | 1.22x |
| sim | steady | 1.32x | 2.00x |
