"""
Defines Django views for 'apps.api' app for competitions
"""
import json
import logging
import os
from datetime import timedelta

from uuid import uuid4

from django.utils import timezone
from django.utils.text import slugify
from rest_framework import (permissions, status, viewsets, views, filters)
from django_filters.rest_framework import DjangoFilterBackend
# from rest_framework.decorators import action, link, permission_classes
from rest_framework.decorators import permission_classes, list_route, detail_route
from rest_framework.exceptions import PermissionDenied, ParseError
from rest_framework.response import Response

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
# from django.contrib import contenttypes as ContentType
from django.contrib.sites.models import Site
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.http import Http404
from django.template import Context
from django.template.loader import render_to_string
from django.utils.decorators import method_decorator
from django.utils.html import escape

from apps.api import serializers
from apps.jobs.models import Job
from apps.web import models as webmodels
from apps.teams import models as teammodels
from apps.web.models import CompetitionSubmission
from apps.web.tasks import (create_competition, evaluate_submission)
from apps.web.utils import BundleStorage

from codalab.azure_storage import make_blob_sas_url, PREFERRED_STORAGE_X_MS_VERSION

logger = logging.getLogger(__name__)



def _generate_blob_sas_url(prefix, extension, name='blob'):
    """
    Helper to generate SAS URL for creating a BLOB.
    """
    blob_name = '{0}/{1}/{2}{3}'.format(prefix, str(uuid4()), name, extension)
    if settings.USE_GCS:
        bucket = BundleStorage.client.get_bucket(settings.GS_PRIVATE_BUCKET_NAME)
        url = bucket.blob(blob_name).generate_signed_url(expiration=timezone.now() + timedelta(seconds=6000), method='PUT', content_type='application/zip')
        logger.debug("_generate_blob_sas_url: sas=%s; blob_name=%s.", url, blob_name)
        return {'url': url, 'id': blob_name}
    else:
        url = make_blob_sas_url(settings.BUNDLE_AZURE_ACCOUNT_NAME,
                                settings.BUNDLE_AZURE_ACCOUNT_KEY,
                                settings.BUNDLE_AZURE_CONTAINER,
                                blob_name,
                                permission='w',
                                duration=6000)
        logger.debug("_generate_blob_sas_url: sas=%s; blob_name=%s.", url, blob_name)
        return {'url': url, 'id': blob_name, 'version': PREFERRED_STORAGE_X_MS_VERSION}


@permission_classes((permissions.IsAuthenticated,))
class CompetitionCreationSasApi(views.APIView):
    """
    Provides a web API to start the process of creating a competition.
    """
    def post(self, request):
        """
        Provides a Blob SAS that a client can use to upload the competition definition bundle.
        Returns a dictionary of the form: { 'url': <shared-access-url>, 'id': <tracking-id> }
        """
        prefix = 'competition/upload/{0}'.format(request.user.id)
        response_data = _generate_blob_sas_url(prefix, '.zip')
        return Response(response_data, status=status.HTTP_201_CREATED)


@permission_classes((permissions.IsAuthenticated,))
class CompetitionCreationApi(views.APIView):
    """
    Provides a web API to continue the process of creating a competition.
    """
    def post(self, request):
        """
        This POST method expects a file identified by the key 'file' in the set of files uploaded
        by the client in multipart MIME format ('multipart/form-data'). The uploaded file is used
        to create a competition definition bundle on behalf of the logged in user. When the bundle
        is created a job is launched to start the process of creating the competition from the
        specified definition. The job ID is returned to the client in a JSON object:
            { 'token': <value> }
        Use the token with CompetitionCreationStatusApi to track the progress of the job.
        """
        blob_name = request.data['id'] if 'id' in request.data else ''
        if len(blob_name) <= 0:
            return Response("Invalid or missing tracking ID.", status=status.HTTP_400_BAD_REQUEST)
        owner = self.request.user
        logger.debug("CompetitionCreation: owner=%s; filename=%s.", owner.id, blob_name)
        cdb = webmodels.CompetitionDefBundle.objects.create(owner=owner)
        cdb.config_bundle.name = blob_name
        cdb.save()
        logger.debug("CompetitionCreation def: owner=%s; def=%s; blob=%s.", owner.id, cdb.pk, cdb.config_bundle.name)
        # Make up a job for this, although we've removed that old system...
        job = Job.objects.create_job('create_competition', {'comp_def_id': cdb.pk})
        create_competition.apply_async((job.pk, cdb.pk,))
        return Response({'token': job.pk}, status=status.HTTP_201_CREATED)


@permission_classes((permissions.IsAuthenticated,))
class CompetitionCreationStatusApi(views.APIView):
    """
    Provides a web API to track progress of a 'create' operation started with CompetitionCreationApi.
    """
    def get(self, request, token):
        """
        Returns the operation status:
           { 'status': <value> }
        where <value> is status of the job as defined by the 'code_name' in apps.jobs.models.Job.STATUS_BY_CODE.
        """
        print("Competition Creation API called")
        user_id = self.request.user.id
        logger.debug("CompetitionCreationStatus: requestor=%s; token=%s.", user_id, token)
        try:
            job = Job.objects.get(pk=token)
        except Job.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        logger.debug("CompetitionCreationStatus: requestor=%s; job=%s; job.status:%s.", user_id, job.pk, job.status)
        data = {'status' : job.get_status_code_name()}
        info = job.get_task_info()
        logger.debug("CompetitionCreationStatus: info=%s", info)
        if 'competition_id' in info:
            data['id'] = info['competition_id']
        if 'error' in info:
            data['error'] = info['error']
        return Response(data)


class CompetitionAPIViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionSerial
    queryset = webmodels.Competition.objects.all()
    filter_class = serializers.CompetitionFilter
    filter_backends = (DjangoFilterBackend,filters.SearchFilter,)
    filter_fields = ('creator')
    search_fields = ("title", "description", "=creator__username")

    # @method_decorator(login_required)
    @permission_classes(permissions.IsAuthenticated,)
    def destroy(self, request, pk, *args, **kwargs):
        """
        Cleanup the destruction of a competition.

        This requires removing phases, submissions, and participants. We should try to design
        the models to make the cleanup simpler if we can.
        """
        # Get the competition
        c = webmodels.Competition.objects.get(id=pk)

        # Create a blank response
        response = {}
        if self.request.user == c.creator:
            c.delete()
            response['id'] = pk
        else:
            response['status'] = 403

        return Response(json.dumps(response), content_type="application/json")

    # @action(methods=['GET'], permission_classes=[permissions.IsAuthenticated])
    @detail_route(methods=['GET'], permission_classes=[permissions.IsAuthenticated])
    def publish(self, request, pk):
        """
        Publish a competition.
        """
        c = webmodels.Competition.objects.get(id=pk)
        response = {}
        if self.request.user == c.creator or self.request.user in c.admins.all():
            # phases_needing_reference_data = webmodels.CompetitionPhase.objects.filter(competition=c, reference_data='').count()

            # if phases_needing_reference_data > 0:
            #     response = {
            #         "error": "Not all phases have reference data, it is required for each phase before publishing."
            #     }
            #     return Response(json.dumps(response), status=400, content_type="application/json")

            c.published = True
            c.save()
            response['id'] = pk
            response['status'] = 200
        else:
            response['status'] = 403
        return Response(json.dumps(response), content_type="application/json")

    # @action(methods=['GET'], permission_classes=[permissions.IsAuthenticated])
    @detail_route(methods=['GET'], permission_classes=[permissions.IsAuthenticated])
    def unpublish(self, request, pk):
        """
        Unpublish a competition.
        """
        c = webmodels.Competition.objects.get(id=pk)
        response = {}
        if self.request.user == c.creator:
            c.published = False
            c.save()
            response['id'] = pk
            response['status'] = 200
        else:
            response['status'] = 403
        return Response(json.dumps(response), content_type="application/json")

    def _send_mail(self, context_data, from_email=None, html_file=None, text_file=None, subject=None, to_email=None):
        from_email = from_email if from_email else settings.DEFAULT_FROM_EMAIL

        context_data["site"] = Site.objects.get_current()
        context = context_data
        text = render_to_string(text_file, context)
        html = render_to_string(html_file, context)

        message = EmailMultiAlternatives(subject, text, from_email, [to_email])
        message.attach_alternative(html, 'text/html')
        message.send()

    # @action(permission_classes=[permissions.IsAuthenticated])
    @detail_route(permission_classes=[permissions.IsAuthenticated], methods=['POST'])
    def participate(self, request, pk=None):
        comp = self.get_object()

        # If there is no registration required we just check to make sure they have agreed to the terms and conditions
        # which is done by the javascript before the ajax call, during form validation.
        if comp.has_registration:
            status = webmodels.ParticipantStatus.objects.get(codename=webmodels.ParticipantStatus.PENDING)
        else:
            status = webmodels.ParticipantStatus.objects.get(codename=webmodels.ParticipantStatus.APPROVED)

        p, cr = webmodels.CompetitionParticipant.objects.get_or_create(user=self.request.user,
                                                                       competition=comp,
                                                                       defaults={'status': status, 'reason': None})

        response_data = {
            'result' : 201 if cr else 200,
            'id' : p.id
        }

        status_text = str(status)

        if status_text.lower() == webmodels.ParticipantStatus.PENDING.lower():
            if self.request.user.participation_status_updates:
                self._send_mail(
                    {
                        'competition': comp,
                        'user': self.request.user,
                    },
                    subject='Application to %s sent' % comp,
                    html_file="emails/notifications/participation_requested.html",
                    text_file="emails/notifications/participation_requested.txt",
                    to_email=self.request.user.email
                )

            if comp.creator.organizer_status_updates:
                self._send_mail(
                    {
                        'competition': comp,
                        'participant': p,
                        'user': comp.creator,
                    },
                    subject='%s applied to your competition' % p.user,
                    html_file="emails/notifications/organizer_participation_requested.html",
                    text_file="emails/notifications/organizer_participation_requested.txt",
                    to_email=comp.creator.email
                )
        elif status_text == webmodels.ParticipantStatus.APPROVED:
            if self.request.user.participation_status_updates:
                self._send_mail(
                    {
                        'competition': comp,
                        'user': self.request.user,
                    },
                    subject='Accepted into %s!' % comp,
                    html_file="emails/notifications/participation_accepted.html",
                    text_file="emails/notifications/participation_accepted.txt",
                    to_email=self.request.user.email
                )

            if comp.creator.organizer_status_updates:
                self._send_mail(
                    {
                        'competition': comp,
                        'participant': p,
                        'user': comp.creator
                    },
                    subject='%s accepted into your competition!' % p.user,
                    html_file="emails/notifications/organizer_participation_accepted.html",
                    text_file="emails/notifications/organizer_participation_accepted.txt",
                    to_email=comp.creator.email
                )

        return Response(json.dumps(response_data), content_type="application/json")

    def _get_userstatus(self, request, pk=None, participant_id=None):
        comp = self.get_object()
        resp = {}
        status = 200
        try:
            p = webmodels.CompetitionParticipant.objects.get(user=self.request.user, competition=comp)
            resp = {'status': p.status.codename, 'reason': p.reason}
        except ObjectDoesNotExist:
            resp = {'status': None, 'reason': None}
            status = 400
        return Response(resp, status=status)

    # @link(permission_classes=[permissions.IsAuthenticated])
    # @permission_classes((permissions.IsAuthenticated,))
    @detail_route(permission_classes=[permissions.IsAuthenticated], methods=['GET'])
    def mystatus(self, request, pk=None):
        return self._get_userstatus(request, pk)

    # @action(methods=['POST', 'PUT'], permission_classes=[permissions.IsAuthenticated])
    @detail_route(methods=['POST', 'PUT'], permission_classes=[permissions.IsAuthenticated])
    def participation_status(self, request, pk=None):
        comp = self.get_object()
        resp = {}
        status = request.data['status']
        participant_id = request.data['participant_id']
        reason = request.data['reason']

        if comp.creator != request.user and request.user not in comp.admins.all():
            raise PermissionDenied()

        try:
            participant = webmodels.CompetitionParticipant.objects.get(competition=comp, pk=participant_id)
            participant.status = webmodels.ParticipantStatus.objects.get(codename=status)
            participant.reason = reason
            participant.save()
            resp = {
                'status': status,
                'participantId': participant_id,
                'reason': reason
                }

            if status == webmodels.ParticipantStatus.PENDING:
                pass
            elif status == webmodels.ParticipantStatus.APPROVED:
                if participant.user.participation_status_updates:
                    self._send_mail(
                        {
                            'competition': comp,
                            'user': participant.user,
                        },
                        subject='Accepted into %s!' % comp,
                        html_file="emails/notifications/participation_accepted.html",
                        text_file="emails/notifications/participation_accepted.txt",
                        to_email=participant.user.email
                    )

                if comp.creator.organizer_status_updates:
                    self._send_mail(
                        {
                            'competition': comp,
                            'participant': participant,
                            'user': comp.creator,
                        },
                        subject='%s accepted into your competition!' % participant.user,
                        html_file="emails/notifications/organizer_participation_accepted.html",
                        text_file="emails/notifications/organizer_participation_accepted.txt",
                        to_email=comp.creator.email
                    )
            elif status == webmodels.ParticipantStatus.DENIED:
                if participant.user.participation_status_updates:
                    self._send_mail(
                        {
                            'competition': comp,
                            'user': participant.user,
                        },
                        subject='Permission revoked from %s!' % comp,
                        html_file="emails/notifications/participation_revoked.html",
                        text_file="emails/notifications/participation_revoked.txt",
                        to_email=participant.user.email
                    )

                if comp.creator.organizer_status_updates:
                    self._send_mail(
                        {
                            'competition': comp,
                            'participant': participant,
                            'user': comp.creator,
                        },
                        subject="%s's permission revoked from your competition!" % participant.user,
                        html_file="emails/notifications/organizer_participation_revoked.html",
                        text_file="emails/notifications/organizer_participation_revoked.txt",
                        to_email=comp.creator.email
                    )

        except ObjectDoesNotExist as e:
            resp = {
                'status': 400
                }

        return Response(json.dumps(resp), content_type="application/json")

    # @action(methods=['POST', 'PUT'], permission_classes=[permissions.IsAuthenticated])
    @detail_route(methods=['POST', 'PUT'], permission_classes=[permissions.IsAuthenticated])
    def team_status(self, request, pk=None):
        comp = self.get_object()
        resp = {}
        status = request.data['status']
        teamID = request.data['team_id']
        reason = request.data['reason']

        if comp.creator != request.user and request.user not in comp.admins.all():
            raise PermissionDenied()

        try:
            team = teammodels.Team.objects.get(competition=comp, pk=teamID)
            team.status = teammodels.TeamStatus.objects.get(codename=status)
            team.reason = reason
            team.save()
            resp = {
                'status': status,
                'teamId': teamID,
                'reason': reason
            }
        except ObjectDoesNotExist as e:
            resp = {
                'status': 400
            }
        return Response(json.dumps(resp), content_type="application/json")

    # @action(permission_classes=[permissions.IsAuthenticated])
    @detail_route(permission_classes=[permissions.IsAuthenticated], methods=['GET'])
    def info(self, request, *args, **kwargs):
        comp = self.get_object()
        comp.title = request.data.get('title')
        comp.description = request.data.get('description')
        comp.save()
        return Response({"data": {
                             "title": comp.title,
                             "description": comp.description,
                             "imageUrl": comp.image.url if comp.image else None},
                         "published": 3}, status=200)

competition_list = CompetitionAPIViewSet.as_view({'get':'list', 'post': 'participate'})
competition_retrieve = CompetitionAPIViewSet.as_view({'get':'retrieve', 'put':'update', 'patch': 'partial_update'})


class CompetitionParticipantAPIViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionParticipantSerial
    queryset = webmodels.CompetitionParticipant.objects.all()

    def get_queryset(self):
        competition_id = self.kwargs.get('competition_id', None)
        return self.queryset.filter(competition__pk=competition_id)


class CompetitionPhaseAPIViewset(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionPhaseSerial
    queryset = webmodels.Competition.objects.all()

    def get_queryset(self):
        competition_id = self.kwargs.get('pk', None)
        phasenumber = self.kwargs.get('phasenumber', None)
        kw = {}
        if competition_id:
            kw['pk'] = competition_id
        if phasenumber:
            kw['phases__phasenumber'] = phasenumber
        return self.queryset.filter(**kw)

competitionphase_list = CompetitionPhaseAPIViewset.as_view({'get':'list', 'post':'create'})
competitionphase_retrieve = CompetitionPhaseAPIViewset.as_view({'get':'retrieve',
                                                                'put':'update',
                                                                'patch':'partial_update'})


class CompetitionPageViewSet(viewsets.ModelViewSet):
    ## TODO: Turn the custom logic here into a mixin for other content
    serializer_class = serializers.PageSerial
    queryset = webmodels.Page.objects.all()
    _pagecontainer = None
    _pagecontainer_q = None

    def get_queryset(self):
        kw = {}
        if 'competition_id' in self.kwargs:
            kw['container__object_id'] = self.kwargs['competition_id']
            kw['container__content_type'] = self.content_type
        if 'category' in self.kwargs:
            kw['category__codename'] = self.kwargs['category']
        if kw:
            return self.queryset.filter(**kw)
        else:
            return self.queryset

    @classmethod
    def get_content_type(cls, *args, **kwargs):
        return ContentType.objects.get_for_model(webmodels.Competition)

    def dispatch(self, request, *args, **kwargs):
        if 'competition_id' in kwargs:
            self._pagecontainer_q = webmodels.PageContainer.objects.filter(object_id=kwargs['competition_id'],
                                                                           content_type=self.content_type)
        return super(CompetitionPageViewSet, self).dispatch(request, *args, **kwargs)

    @property
    def pagecontainer(self):
        if self._pagecontainer_q is not None and self._pagecontainer is None:
            try:
                self._pagecontainer = self._pagecontainer_q.get()
            except ObjectDoesNotExist:
                self._pagecontainer = None
        return self._pagecontainer

    def new_pagecontainer(self, competition_id):
        try:
            competition = webmodels.Competition.objects.get(pk=competition_id)
        except ObjectDoesNotExist:
            raise Http404
        self._pagecontainer = webmodels.PageContainer.objects.create(object_id=competition_id,
                                                                     content_type=self.content_type)
        return self._pagecontainer

    def get_serializer_context(self):
        ctx = super(CompetitionPageViewSet, self).get_serializer_context()
        if 'competition_id' in self.kwargs:
            ctx.update({'container': self.pagecontainer})
        return ctx

    def create(self, request, *args, **kwargs):
        container = self.pagecontainer
        if not container:
            container = self.new_pagecontainer(self.kwargs.get('competition_id'))
        return  super(CompetitionPageViewSet, self).create(request, *args, **kwargs)

competition_page_list = CompetitionPageViewSet.as_view({'get':'list', 'post':'create'})
competition_page = CompetitionPageViewSet.as_view({'get':'retrieve', 'put':'update', 'patch':'partial_update'})


@permission_classes((permissions.IsAuthenticated,))
class CompetitionSubmissionSasApi(views.APIView):
    """
    Provides a web API to start the process of making a submission to a competition.
    """
    def post(self, request, competition_id=''):
        """
        Provides a Blob SAS that a client can use to upload a submission.
        Returns a dictionary of the form: { 'url': <shared-access-url>, 'id': <tracking-id> }
        """
        if len(competition_id) <= 0:
            raise ParseError(detail='Invalid competition ID.')
        name, _ = os.path.splitext(request.POST.get('name'))
        prefix = 'competition/{0}/submission/{1}'.format(competition_id, request.user.id)
        response_data = _generate_blob_sas_url(prefix, '.zip', name=name)
        return Response(response_data, status=status.HTTP_201_CREATED)


@permission_classes((permissions.IsAuthenticated,))
class CompetitionSubmissionViewSet(viewsets.ModelViewSet):
    queryset = webmodels.CompetitionSubmission.objects.all()
    serializer_class = serializers.CompetitionSubmissionSerial
    _file = None

    def get_queryset(self):
        return self.queryset.filter(phase__competition__pk=self.kwargs['competition_id'])

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        temp_obj = self.perform_create(serializer)
        temp_dict = {
            'id': temp_obj.id,
            'description': temp_obj.description,
            'status': temp_obj.status.codename,
            'submission_number': temp_obj.submission_number,
            'submitted_at': temp_obj.submitted_at.isoformat()
        }
        headers = self.get_success_headers(serializer.data)
        # new_data = json.dumps(temp_dict)
        return Response(temp_dict, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        # obj = CompetitionSubmission()
        kwargs = {}
        try:
            kwargs['participant'] = webmodels.CompetitionParticipant.objects.get(
                competition=self.kwargs['competition_id'], user=self.request.user)
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not kwargs['participant'].is_approved:
            raise PermissionDenied()
        phase_id = self.request.query_params.get('phase_id', "")
        submission_phase = webmodels.CompetitionPhase.objects.filter(
            competition=self.kwargs['competition_id'],
            id=phase_id
        ).first()
        if submission_phase is None or submission_phase.is_active is False:
            raise PermissionDenied(detail='Competition phase is closed.')
        if submission_phase.auto_migration and not submission_phase.is_migrated and not submission_phase.competition.is_migrating_delayed:
            raise PermissionDenied(
                detail="Failed, competition phase is being migrated, please try again in a few minutes")

        return CompetitionSubmission.create_submission(self.request, submission_phase, **kwargs)

    def handle_exception(self, exc):
        if type(exc) is DjangoPermissionDenied:
            exc = PermissionDenied(detail=str(exc))
        return super(CompetitionSubmissionViewSet, self).handle_exception(exc)

    # @action(methods=["DELETE"])
    @detail_route(methods=['DELETE'], permission_classes=[permissions.IsAuthenticated])
    def removeFromLeaderboard(self, request, pk=None, competition_id=None):
        try:
            participant = webmodels.CompetitionParticipant.objects.filter(competition=self.kwargs['competition_id'],
                                                                          user=self.request.user).get()
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not participant.is_approved:
            raise PermissionDenied()
        submission = webmodels.CompetitionSubmission.objects.get(id=pk)
        if not submission.phase.is_active:
            raise PermissionDenied(detail='Competition phase is closed.')
        if submission.phase.is_blind:
            raise PermissionDenied(detail='Competition phase does not allow participants to modify the leaderboard.')
        if submission.participant.user != self.request.user:
            raise ParseError(detail='Invalid submission')
        try:
            response = dict()
            lb = webmodels.PhaseLeaderBoard.objects.get(phase=submission.phase)
            lbe = webmodels.PhaseLeaderBoardEntry.objects.get(board=lb, result=submission)
            lbe.delete()
            response['status'] = lbe.id
            return Response(response, status=response['status'], content_type="application/json")
        except ObjectDoesNotExist:
            raise PermissionDenied()

    # @action(methods=["POST"])
    @detail_route(methods=['POST'], permission_classes=[permissions.IsAuthenticated])
    def addToLeaderboard(self, request, pk=None, competition_id=None):
        try:
            participant = webmodels.CompetitionParticipant.objects.filter(competition=self.kwargs['competition_id'],
                                                                          user=self.request.user).get()
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not participant.is_approved:
            raise PermissionDenied()
        submission = webmodels.CompetitionSubmission.objects.get(id=pk)
        if not submission.phase.is_active:
            raise PermissionDenied(detail='Competition phase is closed.')
        if submission.phase.is_blind:
            raise PermissionDenied(detail='Competition phase does not allow participants to modify the leaderboard.')
        if submission.participant.user != self.request.user:
            raise ParseError(detail='Invalid submission')
        response = dict()
        _, cr = webmodels.add_submission_to_leaderboard(submission)
        response['status'] = (201 if cr else 200)
        return Response(response, status=response['status'], content_type="application/json")

competition_submission_retrieve = CompetitionSubmissionViewSet.as_view({'get':'retrieve'})
competition_submission_create = CompetitionSubmissionViewSet.as_view({'post':'create'})
competition_submission_leaderboard = CompetitionSubmissionViewSet.as_view(
                                        {'post':'addToLeaderboard', 'delete':'removeFromLeaderboard'})


class LeaderBoardViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.LeaderBoardSerial
    queryset = webmodels.PhaseLeaderBoard.objects.all()

    def get_queryset(self):
        kw = {}
        competition_id = self.kwargs.get('competition_id', None)
        phase_id = self.kwargs.get('phase_id', None)
        if phase_id:
            kw['phase__pk'] = phase_id
        if competition_id:
            kw['phase__competition__pk'] = competition_id
        return self.queryset.filter(**kw)

leaderboard_list = LeaderBoardViewSet.as_view({'get':'list', 'post':'create'})
leaderboard_retrieve = LeaderBoardViewSet.as_view({'get':'retrieve', 'put':'update', 'patch':'partial_update'})


class LeaderBoardDataViewSet(views.APIView):
    """
    Provides a web API to get the leaderboard data for a phase of a competition
    """
    def get(self, request, *args, **kwargs):
        competition_id = self.kwargs.get('competition_id', None)
        phase_id = self.kwargs.get('phase_id', None)
        competition = webmodels.Competition.objects.get(pk=competition_id)
        phase = webmodels.CompetitionPhase.objects.filter(competition=competition, phasenumber=phase_id)[0]
        if phase.is_blind:
            return Response(status=403)
        groups = phase.scores()
        response = Response(groups, status=status.HTTP_200_OK)
        return response


class DefaultContentViewSet(viewsets.ModelViewSet):
    queryset = webmodels.DefaultContentItem.objects.all()
    serializer_class = serializers.DefaultContentSerial
