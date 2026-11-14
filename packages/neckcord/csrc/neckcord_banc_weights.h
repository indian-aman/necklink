/* Auto-generated from BANC v888 Adult Female Drosophila Whole-CNS (157789 neurons, 23553107 synapses).
 * connectome_compiled.npz SHA-256: adf507c8e402ecc19920d8a27cce01468215ff2f1e8396dd1fea2d1387f33b9e
 * neurons_meta.parquet    SHA-256: 781e0f66d9bf7319d1241a2f7cc9f99615b34a0c6d3c6eba6b9002d12f772525
 * DO NOT EDIT BY HAND - regenerate via `python -m necklink_cns.connectome.extract`.
 */
#ifndef NECKCORD_BANC_WEIGHTS_H
#define NECKCORD_BANC_WEIGHTS_H

#include <stdint.h>

typedef struct {
    uint16_t C_vnc_inh_q12[36];
    int16_t  W_dn_leg_q12[60];
} nc_banc_vnc_weights_t;

static const nc_banc_vnc_weights_t k_nc_banc_vnc_default = {
  {
    0u, 1790u, 5931u, 1363u, 4942u, 1319u,
    1790u, 0u, 1711u, 6428u, 2376u, 4498u,
    5931u, 1711u, 0u, 2506u, 9599u, 2266u,
    1363u, 6428u, 2506u, 0u, 3241u, 8213u,
    4942u, 2376u, 9599u, 3241u, 0u, 5258u,
    1319u, 4498u, 2266u, 8213u, 5258u, 0u
},
  {
    1268, -50, 1547, -365, 3424, 15, -1, 452, -1347, -95,
    560, 1616, -781, 1290, 75, 4096, 484, 108, 340, -275,
    4096, -121, 371, -1979, 466, 21, 3892, 319, -2618, 271,
    162, 1743, -1049, 410, 62, 201, 1039, 4096, 14, -1020,
    2190, 72, 4096, 304, 1169, 43, 1014, -487, -496, 1048,
    -158, 677, 1149, 2130, 8, 1294, -859, 265, -4096, 477
}
};

#endif /* NECKCORD_BANC_WEIGHTS_H */
