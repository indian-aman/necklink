/* Auto-generated from BANC v888 Adult Female Drosophila Whole-CNS (157789 neurons, 23553107 synapses).
 * connectome_compiled.npz SHA-256: adf507c8e402ecc19920d8a27cce01468215ff2f1e8396dd1fea2d1387f33b9e
 * neurons_meta.parquet    SHA-256: 781e0f66d9bf7319d1241a2f7cc9f99615b34a0c6d3c6eba6b9002d12f772525
 * DO NOT EDIT BY HAND - regenerate via `python -m necklink_cns.connectome.extract`.
 */
#ifndef NECKLINK_CNS_BANC_WEIGHTS_H
#define NECKLINK_CNS_BANC_WEIGHTS_H

#include <stdint.h>

typedef struct {
    int16_t W_brain_dn_q12[110]; /* 10 Core DNs x 11 Brain Output Populations */
    int16_t W_dn_leg_q12[60];    /* 6 Thoracic Leg MN Pools x 10 Core DNs */
} nl_banc_dn_weights_t;

static const nl_banc_dn_weights_t k_nl_banc_dn_default = {
  {
    1794, 115, 1451, -230, 2322, 0, 0, 0, 0, -2, -56,
    135, 4096, -360, 4096, 133, 777, -1, -1, 0, -1, -130,
    2, 1, 6, 16, -57, 0, -659, -699, 4096, 1565, -4096,
    0, 0, 7, 5, 0, -41, -637, -263, 1766, 1852, -3158,
    0, 0, 1, 15, -25, 0, -389, 4096, 1608, 1168, -2019,
    0, 0, 16, 2, 2, 378, 3028, -495, 1217, 749, -1256,
    1, 3, 10, 62, 688, 0, -123, 61, -84, 57, -143,
    2, 0, 78, 18, 975, 1084, 64, 6, -7, 59, -36,
    -70, 0, 0, 0, 1315, 0, 0, 0, 0, -21, -42,
    1, -38, -1, -2, 0, 4096, 0, 0, 0, 14, -60
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

#endif /* NECKLINK_CNS_BANC_WEIGHTS_H */
