'''
Arvados Platform class
'''
import json
import logging
import os
import re
import subprocess
import tempfile

import arvados

logger = logging.getLogger(__name__)

class ArvadosTask():
    '''
    Arvados Task class to encapsulate task functionality to mimick SevenBrides task class
    '''
    def __init__(self, container_request, container):
        self.container_request = container_request
        self.container = container

    def to_dict(self):
        ''' Convert to dictionary '''
        return {
            'container_request': self.container_request,
            'container': self.container
        }

    @classmethod
    def from_dict(cls, task_dict):
        ''' Convert from dictionary '''
        return cls(task_dict['container_request'], task_dict['container'])

# custom JSON encoder
class ArvadosTaskEncoder(json.JSONEncoder):
    ''' Arvados Task Encoder class '''
    def default(self, o):
        ''' Default '''
        if isinstance(o, ArvadosTask):
            return o.to_dict()
        return super().default(o)

# custom JSON decoder
def arvados_task_decoder(obj):
    ''' Arvados Task Decoder class '''
    if 'container_request' in obj and 'container' in obj:
        return ArvadosTask(obj['container_request'], obj['container'])
    return obj

class ArvadosPlatform():
    ''' Arvados Platform class '''
    def __init__(self):
        self.api_config = arvados.config.settings()
        self.api = None
        self.keep_client = None

    def _get_files_list_in_collection(self, collection_uuid, subdirectory_path=None):
        '''
        Get list of files in collection, if subdirectory_path is provided, return only files in that subdirectory.

        :param collection_uuid: uuid of the collection
        :param subdirectory_path: subdirectory path to filter files in the collection
        :return: list of files in the collection
        '''
        the_col = arvados.collection.CollectionReader(manifest_locator_or_text=collection_uuid)
        file_list = the_col.all_files()
        if subdirectory_path:
            return [fl for fl in file_list if os.path.basename(fl.stream_name()) == subdirectory_path]
        return list(file_list)

    def _copy_files_from_source_dest_collection(self, source_coll_uuid, dest_coll_uuid, overwrite=False):
        '''
        copies all files from source collection sub-path to target collection sub-path
        When output is True, this copies all files from source collection to target collection sub-path

        :param source_coll_uuid: uuid of the source collection
        :param dest_coll_uuid: uuid of the target collection
        :param overwrite: overwrite the file if it exists in the target collection
        :param output: copy all files from source collection to target collection sub-path
        '''
        source = arvados.collection.CollectionReader(source_coll_uuid)
        target = arvados.collection.Collection(dest_coll_uuid, api_client=self.api, keep_client=self.keep_client)

        # Get all files from source coll without passing sub_path to get_files_list_in_collection
        all_files = self._get_files_list_in_collection(source_coll_uuid)
        for res_file in all_files:
            if res_file.name() not in ['cwl.input.json', 'cwl.output.json']:
                # when copying the output results, sub_path is the sample name,
                # we are re-using it to rename the output file for this usecase
                target_path = f"{res_file.stream_name()}/{res_file.name()}"
                try:
                    # TODO: We need to check if the file exists in the target collection before copying
                    target.copy(target_path,
                                target_path = target_path,
                                source_collection = source,
                                overwrite = overwrite)
                except IOError:
                    continue
        target.save()

    def connect(self):
        ''' Connect to Arvados '''
        self.api = arvados.api_from_config(version='v1', apiconfig=self.api_config)
        self.keep_client = arvados.KeepClient(self.api)

    def copy_folder(self, reference_project, reference_folder, destination_project):
        '''
        Copy reference folder to destination project

        :param reference_project: The reference project
        :param reference_folder: The reference folder
        :param destination_project: The destination project
        :return: The destination folder
        '''
        # The first element of the reference_folder path is the name of the collection.
        if reference_folder.startswith('/'):
            collection_name = reference_folder.split('/')[1]
        else:
            collection_name = reference_folder.split('/')[0]

        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", reference_project["uuid"]],
            ["name", "=", collection_name]
            ]).execute()
        if len(search_result['items']) > 0:
            reference_collection = search_result['items'][0]
        else:
            return None

        # Get the destination project collection
        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", destination_project["uuid"]],
            ["name", "=", collection_name]
            ]).execute()
        if len(search_result['items']) > 0:
            destination_collection = search_result['items'][0]
        else:
            destination_collection = self.api.collections().create(body={
                "owner_uuid": destination_project["uuid"],
                "name": collection_name,
                "description": reference_collection["description"],
                "preserve_version":True}).execute()

        # Copy the files from the reference project to the destination project
        self._copy_files_from_source_dest_collection(reference_collection["uuid"], destination_collection["uuid"])
        return destination_collection

    def copy_reference_data(self, reference_project, destination_project):
        '''
        Copy all data from the reference_project to project, IF the data (by name) does not already
        exist in the project.
        '''
        # Get the reference project "reference_input" collection
        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", reference_project["uuid"]],
            ["name", "=", "reference_input"]
            ]).execute()
        if len(search_result['items']) > 0:
            reference_input_collection = search_result['items'][0]
        else:
            return None

        # Get the destination project "inputs" collection
        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", destination_project["uuid"]],
            ["name", "=", reference_input_collection["name"]]
            ]).execute()
        if len(search_result['items']) > 0:
            destination_input_collection = search_result['items'][0]
        else:
            destination_input_collection = self.api.collections().create(body={
                "owner_uuid": destination_project["uuid"],
                "name": reference_input_collection["name"],
                "description": reference_input_collection["description"],
                "preserve_version":True}).execute()

        # Copy the files from the reference project to the destination project
        self._copy_files_from_source_dest_collection(reference_input_collection["uuid"], destination_input_collection["uuid"])

    def copy_workflow(self, src_workflow, destination_project):
        '''
        Copy a workflow from one project to another, if a workflow with the same name
        does not already exist in the destination project.

        :param src_workflow: The workflow to copy
        :param destination_project: The project to copy the workflow to
        :return: The workflow that was copied or exists in the destination project
        '''
        # Get the workflow we want to copy
        try:
            workflow = self.api.workflows().get(uuid=src_workflow).execute()
        except arvados.errors.ApiError:
            return None

        wf_name = workflow["name"]
        # Check if there is a git version at the end, and if so, strip it
        result = re.search(r' \(.*\)$', wf_name)
        # If the git hasn is present, strip it.
        if result:
            wf_name = wf_name[0:result.start()]

        # Get the existing (if any) workflow in the destination project with the same name as the
        # reference workflow
        existing_workflows = self.api.workflows().list(filters=[
            ["owner_uuid", "=", destination_project["uuid"]],
            ["name", "like", f"{wf_name}%"]
            ]).execute()
        if len(existing_workflows["items"]):
            # Return existing matching workflow
            return existing_workflows["items"][0]

        # Workflow does not exist in project, so copy it
        workflow['owner_uuid'] = destination_project['uuid']
        del workflow['uuid']
        copied_workflow = self.api.workflows().create(body=workflow).execute()
        return copied_workflow

    def copy_workflows(self, reference_project, destination_project):
        '''
        Copy all workflows from the reference_project to project,
        IF the workflow (by name) does not already exist in the project.
        '''
        # Get list of reference workflows
        reference_workflows = self.api.workflows().list(filters=[
            ["owner_uuid", "=", reference_project["uuid"]]
            ]).execute()
        destination_workflows = self.api.workflows().list(filters=[
            ["owner_uuid", "=", destination_project["uuid"]]]
            ).execute()
        # Copy the workflow if it doesn't already exist in the destination project
        for wf in reference_workflows["items"]:
            if wf["name"] not in [wf["name"] for wf in destination_workflows["items"]]:
                wf['owner_uuid'] = destination_project["uuid"]
                del wf['uuid']
                destination_workflows.append(self.api.workflows().create(body=wf).execute())
        return destination_workflows

    def delete_task(self, task):
        ''' Delete a task/workflow/process '''
        self.api.container_requests().delete(uuid=task.container_request["uuid"]).execute()

    @classmethod
    def detect(cls):
        '''
        Detect if we are running in a Arvados environment
        '''
        if os.environ.get('ARVADOS_API_HOST', None):
            return True
        return False

    def get_file_id(self, project, file_path):
        '''
        Get a file id by its full path name
        
        :param project: The project to search
        :param file_path: The full path of the file to search for
        :return: The file id or None if not found
        '''
        if file_path.startswith('http') or file_path.startswith('keep'):
            return file_path

        # Get the collection
        # file_path is assumed to a full path name, starting with a '/'.
        # the first folder in the path is the name of the collection.
        folder_tree = file_path.split('/')
        if not folder_tree[0]:
            folder_tree = folder_tree[1:]

        # The first folder is the name of the collection.
        collection_name = folder_tree[0]
        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", project["uuid"]],
            ["name", "=", collection_name]
            ]).execute()
        if len(search_result['items']) > 0:
            collection = search_result['items'][0]
        else:
            raise Exception(f"Collection {collection_name} not found in project {project['uuid']}")
        # TODO: Add check for file in collection
        return f"keep:{collection['portable_data_hash']}/{'/'.join(folder_tree[1:])}"

    def get_folder_id(self, project, folder_path):
        '''
        Get the folder id in a project

        :param project: The project to search for the file
        :param file_path: The path to the folder
        :return: The file id of the folder
        '''
        # The first folder is the name of the collection.
        collection_name, folder_path = os.path.split(folder_path)
        collection_name = collection_name.lstrip("/")
        search_result = self.api.collections().list(filters=[
            ["owner_uuid", "=", project["uuid"]],
            ["name", "=", collection_name]
            ]).execute()
        if len(search_result['items']) > 0:
            collection = search_result['items'][0]
        else:
            return None
        return f"keep:{collection['uuid']}/{folder_path}"

    def get_task_state(self, task, refresh=False):
        '''
        Get workflow/task state

        :param project: The project to search
        :param task: The task to search for.  Task is a dictionary containing a container_request_uuid and container dictionary.
        :return: The state of the task (Queued, Running, Complete, Failed, Cancelled)
        '''
        if refresh:
            # On newly submitted jobs, we'll only have a container_request, uuid.
            task.container_request = arvados.api().container_requests().get(uuid = task.container_request['uuid']).execute()
            task.container = arvados.api().containers().get(uuid = task.container_request['container_uuid']).execute()

        if task.container['exit_code'] == 0:
            return 'Complete'
        if task.container['exit_code'] == 1:
            return 'Failed'
        if task.container['state'] == 'Running':
            return 'Running'
        if task.container['state'] == 'Cancelled':
            return 'Cancelled'
        if task.container['state'] in ['Locked','Queued']:
            return 'Queued'
        raise Exception(f"TODO: Unknown task state: {task.container['state']}")

    def get_task_output(self, task, output_name):
        ''' Retrieve the output field of the task '''
        cwl_output_collection = arvados.collection.Collection(task.container_request['output_uuid'],
                                                              api_client=self.api,
                                                              keep_client=self.keep_client)
        with cwl_output_collection.open('cwl.output.json') as cwl_output_file:
            cwl_output = json.load(cwl_output_file)
        output_file = cwl_output[output_name]['location']
        output_file_location = f"keep:{task.container_request['output_uuid']}/{output_file}"
        return output_file_location

    def get_tasks_by_name(self, project, task_name):
        '''
        Get all processes (jobs) in a project with a specified name

        :param project: The project to search
        :param process_name: The name of the process to search for
        :return: List of container request uuids and associated containers
        '''
        # We must add priority>0 filter so we do not capture Cancelled jobs as Queued jobs.
        # According to Curii, 'Cancelled' on the UI = 'Queued' with priority=0, we are not interested in Cancelled
        # jobs here anyway, we will submit the job again
        tasks = []
        for container_request in arvados.util.keyset_list_all(
            self.api.container_requests().list,
            filters=[
                ["name", '=', task_name],
                ['owner_uuid', '=', project['uuid']], ['priority', '>', 0]
            ]
        ):
            # Get the container
            container = self.api.containers().get(uuid=container_request['container_uuid']).execute()
            tasks.append(ArvadosTask(container_request, container))
        return tasks

    def get_project(self):
        ''' Determine what project we are running in '''
        try:
            current_container = self.api.containers().current().execute()
            request = self.api.container_requests().list(filters=[
                    ["container_uuid", "=", current_container["uuid"]]
                ]).execute()
            return self.get_project_by_id(request["items"][0]['owner_uuid'])
        except arvados.errors.ApiError:
            return None

    def get_project_by_name(self, project_name):
        ''' Get a project by its name '''
        search_result = self.api.groups().list(filters=[["name", "=", project_name]]).execute()
        if len(search_result['items']) > 0:
            return search_result['items'][0]
        return None

    def get_project_by_id(self, project_id):
        ''' Get a project by its id '''
        search_result = self.api.groups().list(filters=[["uuid", "=", project_id]]).execute()
        if len(search_result['items']) > 0:
            return search_result['items'][0]
        return None

    def submit_task(self, name, project, workflow, parameters):
        ''' Submit a workflow on the platform '''
        with tempfile.NamedTemporaryFile() as parameter_file:
            with open(parameter_file.name, mode='w', encoding="utf-8") as fout:
                json.dump(parameters, fout)

            cmd_str = ['arvados-cwl-runner', '--no-wait',
                    '--defer-download',
                    '--varying-url-params=AWSAccessKeyId,Signature,Expires',
                    '--prefer-cached-downloads',
                    '--debug',
                    '--project-uuid', project['uuid'],
                    '--name', name,
                    workflow['uuid'],
                    parameter_file.name]
            try:
                logger.debug("Calling: %s", " ".join(cmd_str))
                runner_out = subprocess.check_output(cmd_str, stderr = subprocess.STDOUT)
                runner_log = runner_out.decode("UTF-8")
                container_request_uuid = list(filter(None, runner_log.split("\n")))[-1]
                return ArvadosTask({'uuid': container_request_uuid}, None)
            except subprocess.CalledProcessError as err:
                logger.error("ERROR LOG: %s", str(err))
                logger.error("ERROR LOG: %s", err.output)
            except IOError as err:
                logger.error("ERROR LOG: %s", str(err))
        return None
