from pygsheets.spreadsheet import Spreadsheet
from pygsheets.worksheet import Worksheet
from pygsheets.custom_types import ExportType
from pygsheets.exceptions import InvalidArgumentValue, CannotRemoveOwnerError, RequestError

from googleapiclient import discovery
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

import logging
import json
import os
import re


"""
pygsheets.drive
~~~~~~~~~~~~~~

This module provides wrappers for the Google Drive API v3.

"""

PERMISSION_ROLES = ['organizer', 'owner', 'writer', 'commenter', 'reader']
PERMISSION_TYPES = ['user', 'group', 'domain', 'anyone']

_EMAIL_PATTERN = re.compile(r"\"?([-a-zA-Z0-9.`?{}]+@[-a-zA-Z0-9.]+\.\w+)\"?")


class DriveAPIWrapper(object):
    """A simple wrapper for the Google Drive API.

    Various utility and convenience functions to support access to Google Drive files. By default the
    requests will access the users personal drive. Use enable_team_drive(team_drive_id) to connect to a
    TeamDrive instead.

    Only functions used by pygsheet are wrapped. All other functionality can be accessed through the service
    attribute.

    `See reference for details <https://developers.google.com/drive/v3/reference/>`_

    :param http:            HTTP object to make requests with.
    :param data_path:       Path to the drive discovery file.
    """

    def __init__(self, http, data_path, logger=logging.getLogger(__name__)):

        with open(os.path.join(data_path, "drive_discovery.json")) as jd:
            self.service = discovery.build_from_document(json.load(jd), http=http)
        self.team_drive_id = None
        self.logger = logger
        self._spreadsheet_mime_type_query = "mimeType='application/vnd.google-apps.spreadsheet'"
        self.retries = 2

    def enable_team_drive(self, team_drive_id):
        """Access TeamDrive instead of the users personal drive."""
        self.team_drive_id = team_drive_id

    def disable_team_drive(self):
        """Do not access TeamDrive (default behaviour)."""
        self.team_drive_id = None

    def get_update_time(self, file_id):
        """Returns the time this file was last modified in RFC 3339 format."""
        return self._execute_request(self.service.files().get(fileId=file_id, fields='modifiedTime'))['modifiedTime']

    def list(self, **kwargs):
        """Fetch metadata of spreadsheets.

        Fetches a list of all files present in the users drive or TeamDrive. See Google Drive API Reference for
        details.

        `See Files:list for details. <https://developers.google.com/drive/v3/reference/files/list>`_

        :param kwargs:      Standard parameters (see documentation for details).
        :return:            List of metadata.
        """
        result = list()
        response = self._execute_request(self.service.files().list(**kwargs))
        result.extend(response['files'])
        while 'nextPageToken' in response:
            kwargs['pageToken'] = response['nextPageToken']
            response = self._execute_request(self.service.files().list(**kwargs))
            result.extend(response['files'])

        if 'incompleteSearch' in response and response['incompleteSearch']:
            self.logger.warning('Not all files in the corpora %s were searched. As a result '
                                'the response might be incomplete.', kwargs['corpora'])
        return result

    def spreadsheet_metadata(self, query=''):
        """Spreadsheet titles, ids & and parent folder ids.

        The query string can be used to filter the returned metadata.

        `See search parameters docs for details. <https://developers.google.com/drive/v3/web/search-parameters>`_

        :param query:   Can be used to filter the returned metadata.
        """
        if query != '':
            query = query + ' and ' + self._spreadsheet_mime_type_query
        else:
            query = self._spreadsheet_mime_type_query
        if self.team_drive_id:
            return self.list(corpora='teamDrive',
                             teamDriveId=self.team_drive_id,
                             supportsTeamDrives=True,
                             includeTeamDriveItems=True,
                             fields='files(id, name, parents)',
                             q=query)
        else:
            return self.list(fields='files(id, name, parents)',
                             q=query)

    def delete(self, file_id, **kwargs):
        """Delete a file by ID.

        Permanently deletes a file owned by the user without moving it to the trash. If the file belongs to a
        Team Drive the user must be an organizer on the parent. If the input id is a folder, all descendants
        owned by the user are also deleted.

        `Reference. <https://developers.google.com/drive/v3/reference/files/delete>`_

        :param file_id:     The Id of the file to be deleted.
        :param kwargs:      Standard parameters (see documentation for details).
        """
        self._execute_request(self.service.files().delete(fileId=file_id, **kwargs))

    def move_file(self, file_id, old_folder, new_folder, **kwargs):
        """Move a file from one folder to another.

        Requires the current folder to delete it.

        `Reference. <https://developers.google.com/drive/v3/reference/files/update>`_

        :param file_id:     ID of the file which should be moved.
        :param old_folder:  Current location.
        :param new_folder:  Destination.
        :param kwargs:      Optional arguments. See reference for details.
        """
        self._execute_request(self.service.files().update(fileId=file_id, removeParents=old_folder,
                                                          addParents=new_folder, **kwargs))

    def _export_request(self, file_id, mime_type, **kwargs):
        """The export request."""
        return self.service.files().export(fileId=file_id, mimeType=mime_type, **kwargs)

    def export(self, sheet, file_format, path='', filename=None):
        """Download a spreadsheet and store it.

         Exports a Google Doc to the requested MIME type and returns the exported content.

         IMPORTANT: This can at most export files with 10 MB in size!

        Uses one or several export request to download the files. When exporting to CSV or TSV each worksheet is
        exported into a separate file. The API cannot put them into the same file. In this case the worksheet index
        is appended to the file-name.

        `Reference. <https://developers.google.com/drive/v3/reference/files/export>`_

        :param sheet:           The spreadsheet or worksheet to be exported.
        :param file_format:     File format (:class:`ExportType`)
        :param path:            Path to where the file should be stored. (default: current working directory)
        :param filename:        Name of the file. (default: Spreadsheet Id)
        """
        request = None
        tmp = None
        mime_type, file_extension = getattr(file_format, 'value', file_format).split(':')

        if isinstance(sheet, Spreadsheet):
            if (file_format == ExportType.CSV or file_format == ExportType.TSV) and len(sheet.worksheets()) > 1:
                for worksheet in sheet:
                    self.export(worksheet, file_format, path=path, filename=filename + str(worksheet.index))
                return
            else:
                request = self._export_request(sheet.id, mime_type)
        elif isinstance(sheet, Worksheet):
            tmp = sheet.index
            sheet.index = 0
            request = self._export_request(sheet.spreadsheet.id, mime_type)

        import io
        file_name = str(sheet.id) + file_extension if filename is None else filename + file_extension
        fh = io.FileIO(path + file_name, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            logging.info('Download progress: %d%%.', int(status.progress() * 100))
        logging.info('Download finished. File saved in %s.', path + file_name + file_extension)

        if tmp is not None:
            sheet.index = tmp

    def create_permission(self, file_id, role, type, **kwargs):
        """Creates a permission for a file or a TeamDrive.

        `See reference for more details. <https://developers.google.com/drive/v3/reference/permissions/create>`_

        :param file_id:                 The ID of the file or Team Drive.
        :param role:                    The role granted by this permission.
        :param type:                    The type of the grantee.
        :keyword emailAddress:          The email address of the user or group to which this permission refers.
        :keyword domain:                The domain to which this permission refers.
        :parameter allowFileDiscovery:  Whether the permission allows the file to be discovered through search. This is
                                        only applicable for permissions of type domain or anyone.
        :keyword expirationTime:        The time at which this permission will expire (RFC 3339 date-time). Expiration
                                        times have the following restrictions:

                                            * They can only be set on user and group permissions
                                            * The time must be in the future
                                            * The time cannot be more than a year in the future

        :keyword emailMessage:          A plain text custom message to include in the notification email.
        :keyword sendNotificationEmail: Whether to send a notification email when sharing to users or groups.
                                        This defaults to true for users and groups, and is not allowed for other
                                        requests. It must not be disabled for ownership transfers.
        :keyword supportsTeamDrives:    Whether the requesting application supports Team Drives. (Default: False)
        :keyword transferOwnership:     Whether to transfer ownership to the specified user and downgrade
                                        the current owner to a writer. This parameter is required as an acknowledgement
                                        of the side effect. (Default: False)
        :keyword useDomainAdminAccess:  Whether the request should be treated as if it was issued by a
                                        domain administrator; if set to true, then the requester will be granted
                                        access if they are an administrator of the domain to which the item belongs.
                                        (Default: False)
        :return: `Permission Resource <https://developers.google.com/drive/v3/reference/permissions#resource>`_
        """
        if 'supportsTeamDrives' not in kwargs and self.team_drive_id:
            kwargs['supportsTeamDrives'] = True

        if 'emailAddress' in kwargs and 'domain' in kwargs:
            raise InvalidArgumentValue('A permission can only use emailAddress or domain. Do not specify both.')
        if role not in PERMISSION_ROLES:
            raise InvalidArgumentValue('A permission role can only be one of ' + str(PERMISSION_ROLES) + '.')
        if type not in PERMISSION_TYPES:
            raise InvalidArgumentValue('A permission role can only be one of ' + str(PERMISSION_TYPES) + '.')

        body = {
            'kind': 'drive#permission',
            'type': type,
            'role': role
        }

        if 'emailAddress' in kwargs:
            if _EMAIL_PATTERN.match(kwargs['emailAddress']):
                body['emailAddress'] = kwargs['emailAddress']
                del kwargs['emailAddress']
            else:
                raise InvalidArgumentValue("The provided e-mail address doesn't have a valid format: " +
                                           kwargs['emailAddress'] + '.')
        elif 'domain' in kwargs:
            body['domain'] = kwargs['domain']
            del kwargs['domain']

        if 'allowFileDiscovery' in kwargs:
            body['allowFileDiscovery'] = kwargs['allowFileDiscovery']
            del kwargs['allowFileDiscovery']

        if 'expirationTime' in kwargs:
            body['expirationTime'] = kwargs['expirationTime']
            del kwargs['expirationTime']

        return self._execute_request(self.service.permissions().create(fileId=file_id, body=body, **kwargs))

    def list_permissions(self, file_id, **kwargs):
        """List all permissions for the specified file.

        `See reference for more details. <https://developers.google.com/drive/v3/reference/permissions/list>`_

        :param file_id:                     The file to get the permissions for.
        :keyword pageSize:                  Number of permissions returned per request. (Default: all)
        :keyword supportsTeamDrives:        Whether the application supports TeamDrives. (Default: False)
        :keyword useDomainAdminAccess:      Request permissions as domain admin. (Default: False)
        :return: List of `Permission Resources <https://developers.google.com/drive/v3/reference/permissions#resource>`_
        """
        if 'supportsTeamDrives' not in kwargs and self.team_drive_id:
            kwargs['supportsTeamDrives'] = True

        # Ensure that all fields are returned. Default is only id, type & role.
        if 'fields' not in kwargs:
            kwargs['fields'] = '*'

        permissions = list()
        response = self._execute_request(self.service.permissions().list(fileId=file_id, **kwargs))
        permissions.extend(response['permissions'])
        while 'nextPageToken' in response:
            response = self._execute_request(self.service.permissions().list(fileId=file_id,
                                             pageToken=response['nextPageToken'], **kwargs))
            permissions.extend(response['permissions'])
        return permissions

    def delete_permission(self, file_id, permission_id, **kwargs):
        """Deletes a permission.

         `See reference for more details. <https://developers.google.com/drive/v3/reference/permissions/delete>`_

        :param file_id:                 The ID of the file or Team Drive.
        :param permission_id:           The ID of the permission.
        :keyword supportsTeamDrives:    Whether the requesting application supports Team Drives. (Default: false)
        :keyword useDomainAdminAccess:  Whether the request should be treated as if it was issued by a
                                        domain administrator; if set to true, then the requester will be
                                        granted access if they are an administrator of the domain to which
                                        the item belongs. (Default: false)
        """
        if 'supportsTeamDrives' not in kwargs and self.team_drive_id:
            kwargs['supportsTeamDrives'] = True

        try:
            self._execute_request(self.service.permissions().delete(fileId=file_id, permissionId=permission_id, **kwargs))
        except HttpError as error:
            self.logger.exception(str(error))
            if re.search(r'The owner of a file cannot be removed\.', str(error)):
                raise CannotRemoveOwnerError('The owner of a file cannot be removed!')
            else:
                raise

    def _execute_request(self, request):
        """Executes a request.

        On time out error will retry X times, where X is equal to self.retries.

        :param request: The request to be executed.
        :return:        Returns the response of the request.
        """
        for i in range(self.retries):
            try:
                response = request.execute()
            except Exception as e:
                if repr(e).find('timed out') == -1:
                    raise
                if i == self.retries - 1:
                    raise RequestError("Timeout : " + repr(e))
                # print ("Cant connect, retrying ... " + str(i))
            else:
                return response


