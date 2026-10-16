/*
 * A reference FMI 3.0 co-simulation FMU of the tractive system, for testing
 * the vHIL's FMI master (vhil/fmi.py) before IFSSIM's electrical FMU lands
 * (isc-fs/IFSSIM#669). Same contract and physics as the Python stand-in
 * (vhil/plant_ts.py): contacts that follow their coil after a delay, the DC
 * link as a closed-form RC between events, the faults as inputs. Written for
 * this repo from the FMI 3.0 standard's function signatures (no header or
 * code copied); built by the tests with the host's C compiler.
 *
 * Deterministic: no clock, no randomness. Supports fmi3GetFMUState /
 * fmi3SetFMUState; the tests also package it with canGetAndSetFMUState
 * "false" to exercise the master's reset-and-replay path.
 */
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef void *fmi3Instance;
typedef void *fmi3InstanceEnvironment;
typedef void *fmi3FMUState;
typedef uint32_t fmi3ValueReference;
typedef double fmi3Float64;
typedef bool fmi3Boolean;
typedef const char *fmi3String;
typedef int fmi3Status;
enum { fmi3OK = 0, fmi3Warning = 1, fmi3Discard = 2, fmi3Error = 3, fmi3Fatal = 4 };
typedef void (*fmi3LogMessageCallback)(fmi3InstanceEnvironment, fmi3Status, fmi3String, fmi3String);
typedef void (*fmi3IntermediateUpdateCallback)(void);

#define EXPORT __attribute__((visibility("default")))
#define TOKEN "{6f1c0e5a-7a52-4c39-9d1f-ts-reference-1}"

/* Value references (modelDescription.xml). */
enum {
    VR_C_LINK = 1, VR_R_PRECHARGE, VR_R_DISCHARGE, VR_R_INVERTER_BLEED,
    VR_AIR_CLOSE, VR_AIR_OPEN, VR_PRE_CLOSE, VR_PRE_OPEN,
    VR_WELDED_AIR_POS = 10, VR_WELDED_AIR_NEG, VR_PRE_OPEN_CIRCUIT, VR_AIR_EXTERNAL_OPEN,
    VR_C_SCALE, VR_R_LEAK,
    VR_AIR_POS_CMD = 20, VR_AIR_NEG_CMD, VR_PRE_CMD, VR_DISCHARGE_CMD, VR_SDC_CLOSED,
    VR_V_PACK, VR_I_INVERTER_DC,
    VR_V_LINK = 30, VR_AIR_POS_CLOSED, VR_AIR_NEG_CLOSED, VR_PRE_CLOSED,
};

enum { AIR_POS, AIR_NEG, PRE, N_CONTACTS };

typedef struct {
    /* parameters; a resistance <= 0 is "none" */
    double c_link, r_precharge, r_discharge, r_inverter_bleed;
    double air_close, air_open, pre_close, pre_open;
    /* inputs */
    bool cmd[N_CONTACTS], discharge_cmd, sdc_closed;
    bool welded_air_pos, welded_air_neg, pre_open_circuit, air_external_open;
    double c_scale, r_leak, v_pack, i_inverter_dc;
    /* state */
    double t, v_link;
    bool closed[N_CONTACTS];
    bool has_pending[N_CONTACTS];
    double pending_t[N_CONTACTS];
    bool pending_closed[N_CONTACTS];
} State;

typedef struct {
    State s;
} Instance;

static void defaults(State *s) {
    memset(s, 0, sizeof *s);
    s->c_link = 400e-6; s->r_precharge = 690.0; s->r_discharge = 6500.0; s->r_inverter_bleed = 0.0;
    s->air_close = 0.020; s->air_open = 0.010; s->pre_close = 0.010; s->pre_open = 0.005;
    s->c_scale = 1.0;
}

static bool contact(const State *s, int c) {
    if (c == AIR_POS && s->welded_air_pos) return true;
    if (c == AIR_NEG && s->welded_air_neg) return true;
    if ((c == AIR_POS || c == AIR_NEG) && s->air_external_open) return false;
    return s->closed[c];
}

static bool target(const State *s, int c) {
    return s->has_pending[c] ? s->pending_closed[c] : s->closed[c];
}

/* After the inputs changed at s->t: schedule each contact's move. */
static void coils(State *s) {
    double close_d[N_CONTACTS] = { s->air_close, s->air_close, s->pre_close };
    double open_d[N_CONTACTS] = { s->air_open, s->air_open, s->pre_open };
    for (int c = 0; c < N_CONTACTS; c++) {
        bool energised = s->sdc_closed && s->cmd[c];
        if (energised == target(s, c)) continue;
        s->has_pending[c] = false;
        if (energised != s->closed[c]) {
            s->has_pending[c] = true;
            /* integer microseconds, as the stand-in schedules them */
            s->pending_t[c] = s->t + round((energised ? close_d[c] : open_d[c]) * 1e6) / 1e6;
            s->pending_closed[c] = energised;
        }
    }
}

/* The circuit now: kind 0 tied (v = v_eq), 1 hold, 2 rc. */
static int segment(const State *s, double *v_eq, double *tau) {
    if (contact(s, AIR_NEG) && contact(s, AIR_POS)) { *v_eq = s->v_pack; *tau = 0; return 0; }
    double g_charge = 0.0;
    if (contact(s, AIR_NEG) && contact(s, PRE) && !s->pre_open_circuit) g_charge = 1.0 / s->r_precharge;
    double g = g_charge;
    if (!s->sdc_closed || s->discharge_cmd) g += 1.0 / s->r_discharge;
    if (s->r_leak > 0) g += 1.0 / s->r_leak;
    if (s->r_inverter_bleed > 0) g += 1.0 / s->r_inverter_bleed;
    if (g == 0.0) { *v_eq = s->v_link; *tau = INFINITY; return 1; }
    *v_eq = g_charge * s->v_pack / g;
    *tau = s->c_link * s->c_scale / g;
    return 2;
}

static void evolve(State *s, double dt) {
    double v_eq, tau;
    int kind = segment(s, &v_eq, &tau);
    if (kind == 0) s->v_link = v_eq;
    else if (kind == 2 && dt > 0) s->v_link = v_eq + (s->v_link - v_eq) * exp(-dt / tau);
}

static void advance(State *s, double t_end) {
    for (;;) {
        int next = -1;
        for (int c = 0; c < N_CONTACTS; c++)
            if (s->has_pending[c] && s->pending_t[c] <= t_end + 1e-12 &&
                (next < 0 || s->pending_t[c] < s->pending_t[next])) next = c;
        if (next < 0) break;
        double at = s->pending_t[next] > s->t ? s->pending_t[next] : s->t;
        evolve(s, at - s->t);
        s->t = at;
        s->closed[next] = s->pending_closed[next];
        s->has_pending[next] = false;
        evolve(s, 0);
    }
    evolve(s, t_end - s->t);
    s->t = t_end;
}

static double *f64(State *s, fmi3ValueReference vr) {
    switch (vr) {
    case VR_C_LINK: return &s->c_link;
    case VR_R_PRECHARGE: return &s->r_precharge;
    case VR_R_DISCHARGE: return &s->r_discharge;
    case VR_R_INVERTER_BLEED: return &s->r_inverter_bleed;
    case VR_AIR_CLOSE: return &s->air_close;
    case VR_AIR_OPEN: return &s->air_open;
    case VR_PRE_CLOSE: return &s->pre_close;
    case VR_PRE_OPEN: return &s->pre_open;
    case VR_C_SCALE: return &s->c_scale;
    case VR_R_LEAK: return &s->r_leak;
    case VR_V_PACK: return &s->v_pack;
    case VR_I_INVERTER_DC: return &s->i_inverter_dc;
    case VR_V_LINK: return &s->v_link;
    default: return NULL;
    }
}

static bool *boolean(State *s, fmi3ValueReference vr) {
    switch (vr) {
    case VR_WELDED_AIR_POS: return &s->welded_air_pos;
    case VR_WELDED_AIR_NEG: return &s->welded_air_neg;
    case VR_PRE_OPEN_CIRCUIT: return &s->pre_open_circuit;
    case VR_AIR_EXTERNAL_OPEN: return &s->air_external_open;
    case VR_AIR_POS_CMD: return &s->cmd[AIR_POS];
    case VR_AIR_NEG_CMD: return &s->cmd[AIR_NEG];
    case VR_PRE_CMD: return &s->cmd[PRE];
    case VR_DISCHARGE_CMD: return &s->discharge_cmd;
    case VR_SDC_CLOSED: return &s->sdc_closed;
    default: return NULL;
    }
}

EXPORT const char *fmi3GetVersion(void) { return "3.0"; }

EXPORT fmi3Instance fmi3InstantiateCoSimulation(
    fmi3String name, fmi3String token, fmi3String resources, fmi3Boolean visible,
    fmi3Boolean logging, fmi3Boolean eventMode, fmi3Boolean earlyReturn,
    const fmi3ValueReference required[], size_t nRequired, fmi3InstanceEnvironment env,
    fmi3LogMessageCallback log, fmi3IntermediateUpdateCallback update) {
    (void)name; (void)visible; (void)logging; (void)eventMode; (void)earlyReturn;
    (void)required; (void)nRequired; (void)env; (void)log; (void)update;
    if (!token || strcmp(token, TOKEN) != 0) return NULL;
    Instance *in = calloc(1, sizeof *in);
    if (!in) return NULL;
    (void)resources;
    defaults(&in->s);
    return in;
}

EXPORT void fmi3FreeInstance(fmi3Instance i) { free(i); }

EXPORT fmi3Status fmi3EnterInitializationMode(fmi3Instance i, fmi3Boolean tolDefined, fmi3Float64 tol,
                                              fmi3Float64 start, fmi3Boolean stopDefined, fmi3Float64 stop) {
    (void)tolDefined; (void)tol; (void)stopDefined; (void)stop;
    ((Instance *)i)->s.t = start;
    return fmi3OK;
}

EXPORT fmi3Status fmi3ExitInitializationMode(fmi3Instance i) {
    coils(&((Instance *)i)->s);
    return fmi3OK;
}

EXPORT fmi3Status fmi3Terminate(fmi3Instance i) { (void)i; return fmi3OK; }

EXPORT fmi3Status fmi3Reset(fmi3Instance i) {
    defaults(&((Instance *)i)->s);
    return fmi3OK;
}

EXPORT fmi3Status fmi3SetFloat64(fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
                                 const fmi3Float64 v[], size_t nv) {
    State *s = &((Instance *)i)->s;
    if (nv != nvr) return fmi3Error;
    for (size_t k = 0; k < nvr; k++) {
        double *p = f64(s, vr[k]);
        if (!p || vr[k] == VR_V_LINK) return fmi3Error;
        *p = v[k];
    }
    coils(s);
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetFloat64(fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
                                 fmi3Float64 v[], size_t nv) {
    State *s = &((Instance *)i)->s;
    if (nv != nvr) return fmi3Error;
    for (size_t k = 0; k < nvr; k++) {
        double *p = f64(s, vr[k]);
        if (!p) return fmi3Error;
        v[k] = *p;
    }
    return fmi3OK;
}

EXPORT fmi3Status fmi3SetBoolean(fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
                                 const fmi3Boolean v[], size_t nv) {
    State *s = &((Instance *)i)->s;
    if (nv != nvr) return fmi3Error;
    for (size_t k = 0; k < nvr; k++) {
        bool *p = boolean(s, vr[k]);
        if (!p) return fmi3Error;
        *p = v[k];
    }
    coils(s);
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetBoolean(fmi3Instance i, const fmi3ValueReference vr[], size_t nvr,
                                 fmi3Boolean v[], size_t nv) {
    State *s = &((Instance *)i)->s;
    if (nv != nvr) return fmi3Error;
    for (size_t k = 0; k < nvr; k++) {
        switch (vr[k]) {
        case VR_AIR_POS_CLOSED: v[k] = contact(s, AIR_POS); break;
        case VR_AIR_NEG_CLOSED: v[k] = contact(s, AIR_NEG); break;
        case VR_PRE_CLOSED: v[k] = contact(s, PRE); break;
        default: {
            bool *p = boolean(s, vr[k]);
            if (!p) return fmi3Error;
            v[k] = *p;
        }
        }
    }
    return fmi3OK;
}

EXPORT fmi3Status fmi3DoStep(fmi3Instance i, fmi3Float64 t, fmi3Float64 h, fmi3Boolean noSetPrior,
                             fmi3Boolean *eventNeeded, fmi3Boolean *terminate, fmi3Boolean *early,
                             fmi3Float64 *lastTime) {
    (void)noSetPrior;
    State *s = &((Instance *)i)->s;
    if (fabs(t - s->t) > 1e-9) return fmi3Error;
    advance(s, t + h);
    *eventNeeded = false; *terminate = false; *early = false; *lastTime = t + h;
    return fmi3OK;
}

EXPORT fmi3Status fmi3GetFMUState(fmi3Instance i, fmi3FMUState *state) {
    Instance *in = i;
    State *copy = *state ? *state : malloc(sizeof *copy);
    if (!copy) return fmi3Error;
    *copy = in->s;
    *state = copy;
    return fmi3OK;
}

EXPORT fmi3Status fmi3SetFMUState(fmi3Instance i, fmi3FMUState state) {
    Instance *in = i;
    if (!state) return fmi3Error;
    in->s = *(State *)state;
    return fmi3OK;
}

EXPORT fmi3Status fmi3FreeFMUState(fmi3Instance i, fmi3FMUState *state) {
    (void)i;
    if (state && *state) { free(*state); *state = NULL; }
    return fmi3OK;
}
