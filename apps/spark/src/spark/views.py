#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging

from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_http_methods

from desktop.lib.django_util import render
from desktop.lib.exceptions_renderable import PopupException
from desktop.lib.rest.http_client import RestException
from desktop.models import Document

from oozie.views.dashboard import show_oozie_error, check_job_access_permission,\
                                  check_job_edition_permission

from spark import api
from spark.models import get_workflow_output, hdfs_link, SparkScript,\
  create_or_update_script, get_scripts


LOG = logging.getLogger(__name__)


def app(request):
  return render('app.mako', request, {
    'autocomplete_base_url': reverse('beeswax:autocomplete', kwargs={}),
  })


def scripts(request):
  return HttpResponse(json.dumps(get_scripts(request.user, is_design=True)), mimetype="application/json")


@show_oozie_error
def dashboard(request):
  spark_api = api.get(request.fs, request.jt, request.user)

  jobs = spark_api.get_jobs()
  hue_jobs = Document.objects.available(SparkScript, request.user)
  massaged_jobs = spark_api.massaged_jobs_for_json(request, jobs, hue_jobs)

  return HttpResponse(json.dumps(massaged_jobs), mimetype="application/json")


def save(request):
  if request.method != 'POST':
    raise PopupException(_('POST request required.'))

  attrs = {
    'id': request.POST.get('id'),
    'name': request.POST.get('name'),
    'script': request.POST.get('script'),
    'user': request.user,
    'parameters': json.loads(request.POST.get('parameters')),
    'resources': json.loads(request.POST.get('resources')),
    'hadoopProperties': json.loads(request.POST.get('hadoopProperties')),
    'language': json.loads(request.POST.get('language')),
  }
  spark_script = create_or_update_script(**attrs)
  spark_script.is_design = True
  spark_script.save()

  response = {
    'id': spark_script.id,
  }

  return HttpResponse(json.dumps(response), content_type="text/plain")


@show_oozie_error
def stop(request):
  if request.method != 'POST':
    raise PopupException(_('POST request required.'))

  spark_script = SparkScript.objects.get(id=request.POST.get('id'))
  job_id = spark_script.dict['job_id']

  job = check_job_access_permission(request, job_id)
  check_job_edition_permission(job, request.user)

  try:
    api.get(request.fs, request.jt, request.user).stop(job_id)
  except RestException, e:
    raise PopupException(_("Error stopping Pig script.") % e.message)

  return watch(request, job_id)


@show_oozie_error
def run(request):
  if request.method != 'POST':
    raise PopupException(_('POST request required.'))

  attrs = {
    'id': request.POST.get('id'),
    'name': request.POST.get('name'),
    'script': request.POST.get('script'),
    'user': request.user,
    'parameters': json.loads(request.POST.get('parameters')),
    'resources': json.loads(request.POST.get('resources')),
    'hadoopProperties': json.loads(request.POST.get('hadoopProperties')),
    'language': json.loads(request.POST.get('language')),
    'is_design': False
  }

  spark_script = create_or_update_script(**attrs)

  params = request.POST.get('submissionVariables')
  oozie_id = api.get(request.fs, request.jt, request.user).submit(spark_script, params)

  spark_script.update_from_dict({'job_id': oozie_id})
  spark_script.save()

  response = {
    'id': spark_script.id,
    'watchUrl': reverse('spark:watch', kwargs={'job_id': oozie_id}) + '?format=python'
  }

  return HttpResponse(json.dumps(response), content_type="text/plain")


def copy(request):
  if request.method != 'POST':
    raise PopupException(_('POST request required.'))

  spark_script = SparkScript.objects.get(id=request.POST.get('id'))
  spark_script.doc.get().can_edit_or_exception(request.user)

  existing_script_data = spark_script.dict

  owner=request.user
  name = existing_script_data["name"] + _(' (Copy)')
  script = existing_script_data["script"]
  parameters = existing_script_data["parameters"]
  resources = existing_script_data["resources"]
  hadoopProperties = existing_script_data["hadoopProperties"]
  language = existing_script_data["language"]

  script_copy = SparkScript.objects.create()
  script_copy.update_from_dict({
      'name': name,
      'script': script,
      'parameters': parameters,
      'resources': resources,
      'hadoopProperties': hadoopProperties,
      'language': language
  })
  script_copy.save()

  copy_doc = spark_script.doc.get().copy()
  script_copy.doc.add(copy_doc)

  response = {
    'id': script_copy.id,
    'name': name,
    'script': script,
    'parameters': parameters,
    'resources': resources,
    'hadoopProperties': hadoopProperties,
    'language': language
  }

  return HttpResponse(json.dumps(response), content_type="text/plain")


def delete(request):
  if request.method != 'POST':
    raise PopupException(_('POST request required.'))

  ids = request.POST.get('ids').split(",")

  for script_id in ids:
    try:
      spark_script = SparkScript.objects.get(id=script_id)
      spark_script.doc.get().can_edit_or_exception(request.user)
      spark_script.doc.get().delete()
      spark_script.delete()
    except:
      None

  response = {
    'ids': ids,
  }

  return HttpResponse(json.dumps(response), content_type="text/plain")


@show_oozie_error
def watch(request, job_id):
  oozie_workflow = check_job_access_permission(request, job_id)
  logs, workflow_actions = api.get(request.jt, request.jt, request.user).get_log(request, oozie_workflow)
  output = get_workflow_output(oozie_workflow, request.fs)

  workflow = {
    'job_id': oozie_workflow.id,
    'status': oozie_workflow.status,
    'progress': oozie_workflow.get_progress(),
    'isRunning': oozie_workflow.is_running(),
    'killUrl': reverse('oozie:manage_oozie_jobs', kwargs={'job_id': oozie_workflow.id, 'action': 'kill'}),
    'rerunUrl': reverse('oozie:rerun_oozie_job', kwargs={'job_id': oozie_workflow.id, 'app_path': oozie_workflow.appPath}),
    'actions': workflow_actions
  }

  response = {
    'workflow': workflow,
    'logs': logs,
    'output': hdfs_link(output)
  }

  return HttpResponse(json.dumps(response), content_type="text/plain")


def install_examples(request):
  result = {'status': -1, 'message': ''}

  if request.method != 'POST':
    result['message'] = _('A POST request is required.')
  else:
    try:
      result['status'] = 0
    except Exception, e:
      LOG.exception(e)
      result['message'] = str(e)

  return HttpResponse(json.dumps(result), mimetype="application/json")
