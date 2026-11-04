/**
 * CommonJS compatibility entry point (`require('nltp')`).
 * Node.js 20.19+ / 22.12+ / 24+ supports `require(esm)` directly; this CJS shim also works cleanly.
 */
'use strict';

module.exports = require('./index.js');
