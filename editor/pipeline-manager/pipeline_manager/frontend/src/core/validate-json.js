/*
 * Copyright (c) 2022-2025 Antmicro <www.antmicro.com>
 *
 * SPDX-License-Identifier: Apache-2.0
 */

import jsonMap from 'json-source-map';
import jsonlint from 'jsonlint';

// vHIL: JSON.stringify in place of Ajv's own stringify, so the bundle needs
// nothing of Ajv but the runtime helpers its standalone code imports.
const stringify = (value) => JSON.stringify(value);

/**
 * Validates JSON according to a given schema.
 *
 * vHIL: with a validator precompiled for it (src/vhil/build-validators.mjs),
 * looked up by the schema's $id and `reference`; nothing is compiled here,
 * so the editor runs under a `script-src 'self'` CSP (no `new Function`).
 *
 * @param {Object<string, Function>} validators - Precompiled validators by key.
 * @param {Object} schema - Validation schema.
 * @param {Object|string} data - Data to validate.
 * @param {string} reference - Schema entity.
 * @returns {string[]} Validation errors.
 */
export default function validateJSON(validators, schema, data, reference = '') {
    const validate = validators[`${schema.$id}${reference}`];
    if (validate === undefined) {
        return [`Invalid value of "reference" parameter: ${reference} (no precompiled validator for ${schema.$id}${reference})`];
    }

    const isTextFormat = typeof data === 'string';
    let dataJSON;

    try {
        dataJSON = isTextFormat ? jsonlint.parse(data) : data;
    } catch (exception) {
        return [`Not a proper JSON file: ${exception.toString()}`];
    }

    const valid = validate(dataJSON);

    if (valid) {
        return [];
    }

    // Parsing errors messages to a human readable string
    const errors = validate.errors?.map((error) => {
        // It is assumed that the id of the schema is for example `dataflow_schema`
        // Here a prefix is obtained
        const nameOfEntity = schema.$id.replace(/((_params)|(_returns))?(_schema)?(.json)?$/, '');
        const path = `${nameOfEntity}${error.instancePath}`;
        let errorPrefix = '';

        if (isTextFormat) {
            const result = jsonMap.parse(data);
            // 1 is added as the lines are numbered from 0
            const lineStart = result.pointers[error.instancePath].value.line + 1;
            const lineEnd = result.pointers[error.instancePath].valueEnd.line + 1;

            if (lineStart === lineEnd) {
                errorPrefix = `Line ${lineStart} -`;
            } else {
                errorPrefix = `Lines ${lineStart}-${lineEnd} -`;
            }
        }

        switch (error.keyword) {
            case 'enum':
                return `${errorPrefix} ${path} ${error.message} - ${stringify(
                    error.params.allowedValues,
                )}`;
            case 'additionalProperties':
                return `${errorPrefix} ${path} ${error.message} - ${stringify(
                    error.params.additionalProperty,
                )}`;
            case 'const':
                return `${errorPrefix} ${path} ${error.message} - ${stringify(
                    error.params.allowedValue,
                )}`;
            case 'unevaluatedProperties':
                return `${errorPrefix} ${path} ${error.message} - ${stringify(
                    error.params.unevaluatedProperty,
                )}`;
            // Those errors are not informative at all
            case 'not':
            case 'oneOf':
                return '';
            default:
                return `${errorPrefix} ${path} ${error.message}`;
        }
    }) ?? [];

    return errors.filter((err) => err !== '');
}
