##################################################################
# Copyright 2016 OSGeo Foundation,                               #
# represented by PyWPS Project Steering Committee,               #
# licensed under MIT, Please consult LICENSE.txt for details     #
##################################################################


import logging
import os
from lxml import etree
import time
from werkzeug.wrappers import Request
from werkzeug.exceptions import HTTPException
from pywps import WPS, OWS
from pywps.app.basic import xml_response
from pywps.exceptions import NoApplicableCode
import pywps.configuration as config
import pywps.dblog

from pywps.response.status import STATUS
from pywps.response import WPSResponse

LOGGER = logging.getLogger("PYWPS")


class ExecuteResponse(WPSResponse):

    def __init__(self, wps_request, uuid, **kwargs):
        """constructor

        :param pywps.app.WPSRequest.WPSRequest wps_request:
        :param pywps.app.Process.Process process:
        :param uuid: string this request uuid
        """

        super(self.__class__, self).__init__(wps_request, uuid)

        self.process = kwargs["process"]
        self.outputs = {o.identifier: o for o in self.process.outputs}


    def write_response_doc(self, clean=True):
        # TODO: check if file/directory is still present, maybe deleted in mean time

        # check if storing of the status is requested
        if self.status >= STATUS.STORE_AND_UPDATE_STATUS:

            # rebuild the doc and update the status xml file
            self.doc = self._construct_doc()

            try:
                with open(self.process.status_location, 'w') as f:
                    f.write(etree.tostring(self.doc, pretty_print=True, encoding='utf-8').decode('utf-8'))
                    f.flush()
                    os.fsync(f.fileno())

                if self.status >= STATUS.DONE_STATUS and clean:
                    self.process.clean()

            except IOError as e:
                raise NoApplicableCode('Writing Response Document failed with : %s' % e)

    def _process_accepted(self):
        return WPS.Status(
            WPS.ProcessAccepted(self.message),
            creationTime=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime())
        )

    def _process_started(self):
        return WPS.Status(
            WPS.ProcessStarted(
                self.message,
                percentCompleted=str(self.status_percentage)
            ),
            creationTime=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime())
        )

    def _process_paused(self):
        return WPS.Status(
            WPS.ProcessPaused(
                self.message,
                percentCompleted=str(self.status_percentage)
            ),
            creationTime=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime())
        )

    def _process_succeeded(self):
        return WPS.Status(
            WPS.ProcessSucceeded(self.message),
            creationTime=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime())
        )

    def _process_failed(self):
        return WPS.Status(
            WPS.ProcessFailed(
                WPS.ExceptionReport(
                    OWS.Exception(
                        OWS.ExceptionText(self.message),
                        exceptionCode='NoApplicableCode',
                        locater='None'
                    )
                )
            ),
            creationTime=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime())
        )

    def _construct_doc(self):
        doc = WPS.ExecuteResponse()
        doc.attrib['{http://www.w3.org/2001/XMLSchema-instance}schemaLocation'] = \
            'http://www.opengis.net/wps/1.0.0 http://schemas.opengis.net/wps/1.0.0/wpsExecute_response.xsd'
        doc.attrib['service'] = 'WPS'
        doc.attrib['version'] = '1.0.0'
        doc.attrib['{http://www.w3.org/XML/1998/namespace}lang'] = 'en-US'
        doc.attrib['serviceInstance'] = '%s%s' % (
            config.get_config_value('server', 'url'),
            '?service=WPS&request=GetCapabilities'
        )

        if self.status >= STATUS.STORE_STATUS:
            if self.process.status_location:
                doc.attrib['statusLocation'] = self.process.status_url

        # Process XML
        process_doc = WPS.Process(
            OWS.Identifier(self.process.identifier),
            OWS.Title(self.process.title)
        )
        if self.process.abstract:
            process_doc.append(OWS.Abstract(self.process.abstract))
        # TODO: See Table 32 Metadata in OGC 06-121r3
        # for m in self.process.metadata:
        #    process_doc.append(OWS.Metadata(m))
        if self.process.profile:
            process_doc.append(OWS.Profile(self.process.profile))
        process_doc.attrib['{http://www.opengis.net/wps/1.0.0}processVersion'] = self.process.version

        doc.append(process_doc)

        # Status XML
        # return the correct response depending on the progress of the process
        if self.status == STATUS.STORE_AND_UPDATE_STATUS:
            if self.status_percentage == 0:
                self.message = 'PyWPS Process %s accepted' % self.process.identifier
                status_doc = self._process_accepted()
                doc.append(status_doc)
                return doc
            elif self.status_percentage > 0:
                status_doc = self._process_started()
                doc.append(status_doc)
                return doc

        # check if process failed and display fail message
        if self.status_percentage == -1:
            status_doc = self._process_failed()
            doc.append(status_doc)
            return doc

        # TODO: add paused status

        if self.status == STATUS.DONE_STATUS:
            status_doc = self._process_succeeded()
            doc.append(status_doc)

            # DataInputs and DataOutputs definition XML if lineage=true
            if self.wps_request.lineage == 'true':
                try:
                    # TODO: stored process has ``pywps.inout.basic.LiteralInput``
                    # instead of a ``pywps.inout.inputs.LiteralInput``.
                    data_inputs = [self.wps_request.inputs[i][0].execute_xml() for i in self.wps_request.inputs]
                    doc.append(WPS.DataInputs(*data_inputs))
                except Exception as e:
                    LOGGER.error("Failed to update lineage for input parameter. %s", e)

                output_definitions = [self.outputs[o].execute_xml_lineage() for o in self.outputs]
                doc.append(WPS.OutputDefinitions(*output_definitions))

            # Process outputs XML
            output_elements = [self.outputs[o].execute_xml() for o in self.outputs]
            doc.append(WPS.ProcessOutputs(*output_elements))
        return doc

    def call_on_close(self, function):
        """Custom implementation of call_on_close of werkzeug
        TODO: rewrite this using werkzeug's tools
        """
        self._close_functions.push(function)

    @Request.application
    def __call__(self, request):
        doc = None
        try:
            doc = self._construct_doc()
        except HTTPException as httpexp:
            raise httpexp
        except Exception as exp:
            raise NoApplicableCode(exp)

        if self.status >= STATUS.DONE_STATUS:
            self.process.clean()

        return xml_response(doc)
