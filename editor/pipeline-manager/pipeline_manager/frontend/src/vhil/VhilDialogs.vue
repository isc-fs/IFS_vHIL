<!--
vHIL: Commit…, Open PR and the keyboard map, as modal dialogs
(CHANGELOG-VHIL.md): what the shell's Save and PR forms did. Native
<dialog>: focus moves in and back, and Escape closes it.
-->

<template>
    <div class="vhil-dialogs">
    <dialog ref="commitDlg" class="vhil-dialog" aria-labelledby="vhil-commit-title" @close="closed">
        <form class="vhil-form" @submit.prevent="doCommit">
            <h2 id="vhil-commit-title" class="vhil-panel-title">Commit {{ ws.id }}</h2>
            <p class="muted">
                Commits to a branch of the workspace: the system file from the graph, and the
                selected scenario. Run then runs that commit.
            </p>
            <label>Branch
                <input
                    v-model="ws.commit.branch" class="vhil-input mono" required placeholder="feat/…"
                />
            </label>
            <label class="vhil-commit-part">
                <span><input v-model="ws.commit.system" type="checkbox" />
                    System
                    <span class="mono">systems/{{ ws.id }}.yaml</span>
                    <span v-if="ws.dirty" class="vhil-dirty">● edited</span></span>
            </label>
            <label v-if="ws.commit.system">Message
                <textarea v-model="ws.commit.message" class="vhil-input" rows="2" required />
            </label>
            <label v-if="scen.name" class="vhil-commit-part">
                <span><input v-model="ws.commit.scenario" type="checkbox" />
                    Scenario
                    <span class="mono">{{ `systems/${ws.id}.scenarios/${scen.name}.yaml` }}</span>
                    <span v-if="scen.dirty" class="vhil-dirty">● edited</span></span>
            </label>
            <label v-if="scen.name && ws.commit.scenario">Scenario message
                <textarea
                    v-model="ws.commit.scenarioMessage" class="vhil-input" rows="2" required
                />
            </label>
            <p
                v-if="scen.name && ws.commit.scenario && scen.errors.length"
                class="vhil-note vhil-warn"
            >
                ▲ The scenario has {{ scen.errors.length }} error(s): the server refuses it until
                they are fixed (Problems).
            </p>
            <p v-if="errors || warnings" class="vhil-note">
                {{ errors }} error(s), {{ warnings }} warning(s) at the last Check: see Problems.
            </p>
            <div class="vhil-actions">
                <button type="button" class="vhil-btn" :disabled="ws.busy" @click="check">
                    Check
                </button>
                <span class="vhil-spacer" />
                <button type="button" class="vhil-btn" @click="close">Cancel</button>
                <button type="submit" class="vhil-btn --primary" :disabled="ws.busy">
                    Commit to branch
                </button>
            </div>
        </form>
    </dialog>

    <dialog ref="prDlg" class="vhil-dialog" aria-labelledby="vhil-pr-title" @close="closed">
        <form class="vhil-form" @submit.prevent="doPr">
            <h2 id="vhil-pr-title" class="vhil-panel-title">
                Pull request to {{ ws.config.base_branch }}
            </h2>
            <p class="muted">
                From <span class="mono">{{ prBranch }}</span>.
            </p>
            <label>Title
                <input v-model="ws.pr.title" class="vhil-input" required />
            </label>
            <label>Body
                <textarea v-model="ws.pr.body" class="vhil-input" rows="4" />
            </label>
            <p v-if="!ws.config.can_open_pr" class="vhil-note">
                No GitHub credentials on the server (VHIL_GITHUB_TOKEN or the GitHub App):
                committed branches stay in the workspace. Push and open the PR by hand.
            </p>
            <p v-if="prUrl" class="vhil-note">
                Opened <a :href="prUrl" target="_blank" rel="noopener">{{ prUrl }}</a>
            </p>
            <div class="vhil-actions">
                <span class="vhil-spacer" />
                <button type="button" class="vhil-btn" @click="close">Close</button>
                <button
                    type="submit" class="vhil-btn --primary"
                    :disabled="ws.busy || !ws.config.can_open_pr"
                >
                    Open PR
                </button>
            </div>
        </form>
    </dialog>

    <dialog ref="saveDlg" class="vhil-dialog" aria-labelledby="vhil-save-title" @close="closed">
        <form class="vhil-form" @submit.prevent="doSave">
            <h2 id="vhil-save-title" class="vhil-panel-title">Save session as scenario</h2>
            <p class="muted">
                The session's applied ops, each at the virtual time it took effect, as a new
                scenario of {{ ws.id }}: Run replays it exactly, Commit… saves it as
                <span class="mono">systems/{{ ws.id }}.scenarios/{{ saveName || '…' }}.yaml</span>.
            </p>
            <label>Name
                <input
                    v-model.trim="saveName" class="vhil-input mono" required
                    pattern="[a-z0-9][a-z0-9\-]{0,63}" placeholder="tsms-precharge-live"
                />
            </label>
            <div class="vhil-actions">
                <span class="vhil-spacer" />
                <button type="button" class="vhil-btn" @click="close">Cancel</button>
                <button type="submit" class="vhil-btn --primary" :disabled="ws.busy">Save</button>
            </div>
        </form>
    </dialog>

    <dialog ref="keysDlg" class="vhil-dialog" aria-labelledby="vhil-keys-title" @close="closed">
        <h2 id="vhil-keys-title" class="vhil-panel-title">Keyboard shortcuts</h2>
        <table class="vhil-keys">
            <tbody>
                <tr v-for="[keys, what] in SHORTCUTS" :key="keys">
                    <td><kbd>{{ keys }}</kbd></td><td>{{ what }}</td>
                </tr>
            </tbody>
        </table>
        <div class="vhil-actions">
            <span class="vhil-spacer" />
            <button type="button" class="vhil-btn" @click="close">Close</button>
        </div>
    </dialog>
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
import {
    ws, check, commit, openPr, problemCount, saveSession,
} from './workspace.js';
import { replay } from './replay.js';
import { SHORTCUTS } from './shortcuts.js';
import { scen } from './scenarios.js';

export default defineComponent({
    setup() {
        const commitDlg = ref(null);
        const prDlg = ref(null);
        const keysDlg = ref(null);
        const saveDlg = ref(null);
        const prUrl = ref('');
        const saveName = ref('');
        const dialogs = {
            commit: commitDlg, pr: prDlg, shortcuts: keysDlg, 'save-session': saveDlg,
        };

        watch(() => ws.dialog, (name) => {
            Object.entries(dialogs).forEach(([key, dlg]) => {
                if (key === name && !dlg.value.open) dlg.value.showModal();
                else if (key !== name && dlg.value?.open) dlg.value.close();
            });
            if (name === 'pr') prUrl.value = '';
            if (name === 'save-session') saveName.value = `live-run-${replay.id}`;
        });
        const close = () => { ws.dialog = null; };
        const closed = () => {
            if (!Object.values(dialogs).some((d) => d.value?.open)) ws.dialog = null;
        };

        const doCommit = async () => {
            const out = await commit();
            if (out) close();
        };
        const doSave = async () => {
            if (await saveSession(saveName.value)) close();
        };
        const doPr = async () => {
            const out = await openPr();
            if (out) prUrl.value = out.url;
        };

        return {
            ws,
            scen,
            commitDlg,
            prDlg,
            keysDlg,
            saveDlg,
            saveName,
            doSave,
            prUrl,
            close,
            closed,
            check,
            doCommit,
            doPr,
            SHORTCUTS,
            prBranch: computed(() => ws.savedBranch || ws.commit.branch || 'the committed branch'),
            errors: computed(() => problemCount('error')),
            warnings: computed(() => problemCount('warning')),
        };
    },
});
</script>
