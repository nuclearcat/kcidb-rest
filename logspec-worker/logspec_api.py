#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>
# Author: Helen Mae Koike Fornazier <helen.koike@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

# Automatically generates KCIDB issues and incidents from logspec error
# specifications

from copy import deepcopy
import gzip
import hashlib
import json
import requests
import kcidb
import logspec.main


# Configuration tables per object type
test_types = {
    'build': {
        # logspec parser to use
        'parser': 'kbuild',
        # Object id field to match in the incidents table
        'incident_id_field': 'build_id',
        # Additional incident parameters
        'build_valid': False,
    },
    'boot': {
        'parser': 'generic_linux_boot',
        'incident_id_field': 'test_id',
        # Additional incident parameters
        'test_status': 'FAIL',
    },
    'kselftest': {
        'parser': 'test_kselftest',
        'incident_id_field': 'test_id',
        # Additional incident parameters
        'test_status': 'FAIL',
    },
}


def get_logspec_errors(parsed_data, parser):
    """From a logspec output dict, extracts the relevant fields for a
    KCIDB issue definition (only the error definitions without "hidden"
    fields (fields that start with an underscore)) and returns the list
    of errors.
    """

    new_status = None
    errors_list = []
    logspec_version = logspec.main.logspec_version()
    base_dict = {
        'version': logspec_version,
        'parser': parser,
    }
    errors = parsed_data.pop('errors')

    # ----------------------------------------------------------------------
    # Special case handling for failed boot tests
    # ----------------------------------------------------------------------

    if parser == 'generic_linux_boot':
        def create_special_boot_error(summary):
            error_dict = {
                'error_type': 'maestro.linux.kernel.boot',
                'error_summary': summary,
                'signature': parsed_data['_signature'],
                'log_excerpt': '',
                'signature_fields': parsed_data['_signature_fields']
            }
            return {'error': error_dict, **base_dict}

        # Check for unclean boot state
        if parsed_data.get('linux.boot.prompt'):
            error = create_special_boot_error('WARNING: Unclean boot. Reached prompt but marked as failed.')
            errors_list.append(error)
            new_status = 'PASS'

        # Check for incomplete boot process
        elif not parsed_data.get('bootloader.done') or not parsed_data.get('linux.boot.kernel_started'):
            error = create_special_boot_error('Bootloader did not finish or kernel did not start.')
            errors_list.append(error)
            new_status = 'MISS'

    # ----------------------------------------------------------------------
    # Parse errors detected by logspec
    # ----------------------------------------------------------------------

    for error in errors:
        logspec_dict = {}
        logspec_dict.update(base_dict)
        logspec_dict['error'] = {k: v for k, v in vars(error).items()
                                 if v and not k.startswith('_')}
        logspec_dict['error']['signature'] = error._signature
        logspec_dict['error']['log_excerpt'] = error._report
        logspec_dict['error']['signature_fields'] = {
            field: getattr(error, field)
            for field in error._signature_fields}
        errors_list.append(logspec_dict)

    return errors_list, new_status


def new_issue(logspec_error, test_type):
    """Generates a new KCIDB issue object from a logspec error for a
    specific object type.
    Returns the issue as a dict.
    """
    error_copy = deepcopy(logspec_error)
    signature = error_copy['error'].pop('signature')
    comment = ""
    if 'error_summary' in error_copy['error']:
        comment += f" {error_copy['error']['error_summary']}"
    if 'target' in error_copy['error']:
        comment += f" in {error_copy['error']['target']}"
        if 'src_file' in error_copy['error']:
            comment += f" ({error_copy['error']['src_file']})"
        elif 'script' in error_copy['error']:
            comment += f" ({error_copy['error']['script']})"
    comment += f" [logspec:{test_types[test_type]['parser']},{error_copy['error']['error_type']}]"
    issue = {
        'origin': 'maestro',
        'id': f'maestro:{signature}',
        'version': 1,
        'comment': comment,
        'misc': {
            'logspec': error_copy
        },
        # Set culprit_code to True by default
        # OBS: this needs to be reviewed on every logspec upgrade
        'culprit': {
            'code': True,
            'harness': False,
            'tool': False
        }
    }
    if 'build_valid' in test_types[test_type]:
        issue['build_valid'] = test_types[test_type]['build_valid']
    if 'test_status' in test_types[test_type]:
        issue['test_status'] = test_types[test_type]['test_status']
    return issue


def new_incident(result_id, issue_id, test_type, issue_version):
    """Generates a new KCIDB incident object for a specific object type
    from an issue id.
    Returns the incident as a dict.
    """
    id_components = json.dumps([result_id, issue_id, issue_version],
                               sort_keys=True, ensure_ascii=False)
    incident_id = hashlib.sha1(id_components.encode('utf-8')).hexdigest()
    incident = {
        'id': f"maestro:{incident_id}",
        'issue_id': issue_id,
        'issue_version': issue_version,
        test_types[test_type]['incident_id_field']: result_id,
        'comment': "test incident, automatically generated",
        'origin': 'maestro',
        'present': True,
    }
    return incident


def process_log(log_file, parser, start_state):
    """Processes a test log using logspec. The log is first fetched from
    file or URL, then parsed using the logspec parser. The result is
    returned as a list of errors.
    """
    log = None
    with open(log_file, 'rb') as f:
        log = f.read().decode('utf-8')

    if not log:
        # If the log is empty, return an empty list
        raise ValueError("Log file is empty or missing")


    parsed_data = logspec.main.parse_log(log, start_state)
    # return processed data
    return get_logspec_errors(parsed_data, parser)


def generate_issues_and_incidents(result_id, log_file, test_type):
    parsed_data = {
        'issue_node': [],
        'incident_node': [],
    }

    """Generate issues and incidents"""
    start_state = logspec.main.load_parser(test_types[test_type]['parser'])
    parser = test_types[test_type]['parser']
    error_list, new_status = process_log(log_file, parser, start_state)
    for error in error_list:
        if error and error['error'].get('signature'):
            # do not generate issues for error_return_code since they are not
            # fatal, avoid noise.
            if error['error'].get('error_type') == 'linux.kernel.error_return_code':
                continue

            issue = new_issue(error, test_type)
            parsed_data['issue_node'].append(issue)
            issue_id = issue["id"]
            issue_version = issue["version"]
            parsed_data['incident_node'].append(new_incident(result_id, issue_id, test_type, issue_version))

    # Remove duplicate issues
    parsed_data['issue_node'] = list({issue["id"]: issue for issue in parsed_data['issue_node']}.values())

    return parsed_data, new_status
