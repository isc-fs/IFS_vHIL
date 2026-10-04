/* Test fixture for tests/unit/test_candef.py, not firmware source.
 * The .def files beside it are unmodified copies from isc-fs/IFS08-CE-ECU
 * Core/Inc/can/messages/ at dev 544b65107f09d8cf2c5e001293579eec22916bd0;
 * this registry lists them the way that repo's all_messages.inc does. */
#include "vcu_heartbeat.def"        /* 0x100 */
#include "vcu_gps_position.def"     /* 0x508 */
#include "pit_diag_status.def"      /* 0x700 */
#include "pit_diag_health.def"      /* 0x704 */
#include "ams_status.def"           /* 0x4A0 (RX, AMS-owned layout) */
/* #include "not_compiled.def" */
