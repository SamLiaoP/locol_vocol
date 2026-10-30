import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useWorkspace } from './workspace.js';
import { workspaceFrameUrl, workspacePageFromPath } from '../utils/workspace-routes.js';

let state, utils, workspace, events, cleanup;
beforeEach(() => {
    events = {};
    const location = { pathname: '/account', search: '', hash: '', origin: 'http://localhost' };
    vi.stubGlobal('window', { location, history: { pushState: vi.fn(), replaceState: vi.fn() },
        addEventListener: (name, fn) => { events[name] = fn; }, removeEventListener: vi.fn() });
    vi.stubGlobal('document', { title: '' });
    state = Object.fromEntries(Object.entries({ currentView: null, selectedRecording: null,
        isRecording: false, audioBlobURL: null, showUploadModal: false, isUserMenuOpen: true,
        isSidebarCollapsed: false, showTimestampsSimpleView: false, editorAutosave: false,
        audioPlayerPosition: 'bottom' }).map(([key, value]) => [key, { value }]));
    utils = { pausePlayback: vi.fn(), showToast: vi.fn(), selectRecording: vi.fn(), switchToUploadView: vi.fn() };
    workspace = useWorkspace({ ref: value => ({ value }), computed: fn => ({ get value() { return fn(); } }),
        watch: vi.fn(), onMounted: fn => fn(), onUnmounted: fn => { cleanup = fn; } }, state, utils);
});
afterEach(() => vi.unstubAllGlobals());

describe('persistent workspace navigation', () => {
    it('recognizes direct links and only frames known content pages', () => {
        expect(workspace.workspacePage.value).toBe('account');
        expect(workspacePageFromPath('/people/')).toBe('people');
        expect(workspacePageFromPath('/recordings/3')).toBe('recordings');
        expect(workspaceFrameUrl('account', '#about')).toBe('/account?embedded=1#about');
        expect(workspaceFrameUrl('recordings')).toBeNull();
        expect(workspaceFrameUrl('https://elsewhere.test')).toBeNull();
    });
    it('returns to the upload landing page without replacing the document', () => {
        expect(workspace.navigateWorkspace('recordings')).toBe(true);
        expect(state.selectedRecording.value).toBeNull();
        expect(state.currentView.value).toBeNull();
        expect(state.isUserMenuOpen.value).toBe(false);
        expect(window.history.pushState).toHaveBeenCalledWith({ workspace: true }, '', '/?home=1');
        expect(utils.pausePlayback).toHaveBeenCalledOnce();
    });
    it('keeps an unfinished capture intact', () => {
        state.currentView.value = 'recording';
        state.audioBlobURL.value = 'blob:unsaved';
        expect(workspace.navigateWorkspace('people')).toBe(false);
        expect(workspace.workspacePage.value).toBe('account');
        expect(window.history.pushState).not.toHaveBeenCalled();
        expect(utils.showToast).toHaveBeenCalledOnce();
    });
    it('preserves modified link clicks', () => {
        const event = { button: 0, ctrlKey: true, preventDefault: vi.fn() };
        workspace.navigateWorkspaceLink(event, 'people');
        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(window.history.pushState).not.toHaveBeenCalled();
    });
    it('selects a recording from another page and handles a cleared selection', () => {
        utils.onRecordingSelected({ id: 7 });
        expect(workspace.workspacePage.value).toBe('recordings');
        expect(window.history.pushState).toHaveBeenLastCalledWith({ workspace: true }, '', '/recordings/7');
        expect(() => utils.onRecordingSelected(null)).not.toThrow();
    });
    it('rejects messages from other origins and other windows', () => {
        const source = {};
        workspace.workspaceFrame.value = { contentWindow: source };
        events.message({ origin: 'http://evil.test', source, data: { type: 'locol:sidebar' } });
        events.message({ origin: window.location.origin, source: {}, data: { type: 'locol:sidebar' } });
        expect(state.isSidebarCollapsed.value).toBe(false);
        events.message({ origin: window.location.origin, source, data: { type: 'locol:sidebar' } });
        expect(state.isSidebarCollapsed.value).toBe(true);
    });
    it('opens recording links sent by the current content frame', () => {
        const source = {};
        workspace.workspaceFrame.value = { contentWindow: source };
        events.message({ origin: window.location.origin, source, data: { type: 'locol:navigate', recordingId: 9 } });
        expect(utils.selectRecording).toHaveBeenCalledWith({ id: 9 });
    });
    it('removes bridge listeners when the workspace unmounts', () => {
        cleanup();
        expect(utils.onRecordingSelected).toBeNull();
        expect(window.removeEventListener).toHaveBeenCalledTimes(2);
    });
});
