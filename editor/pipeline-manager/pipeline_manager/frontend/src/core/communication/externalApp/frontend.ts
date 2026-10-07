import { JSONRPCRequest } from 'json-rpc-2.0';
import ExternalApp, { EndpointType } from './base';

export default class ExternalFrontendApp implements ExternalApp {
    // eslint-disable-next-line no-useless-constructor
    public constructor(public source: Window) {}

    // eslint-disable-next-line class-methods-use-this
    public async isConnected() { return true; }

    // eslint-disable-next-line class-methods-use-this
    public async onConnect() { return true; }

    public request(data: JSONRPCRequest, endpoint: EndpointType) {
        if (endpoint === 'backend-api') throw new Error('Frontend external app does not support backend endpoints');
        // vHIL: only a same-origin page may drive the editor (custom/Editor.vue
        // drops other origins' messages), so the reply goes to that origin
        // only, never '*'.
        this.source.postMessage(data, window.location.origin);
    }
}
