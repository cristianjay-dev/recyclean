from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.hashers import make_password, check_password
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.middleware.csrf import get_token

from .forms import ManualQuizForm
from .models import RewardRequest, Quiz, QuizAnswer, User, QuizSession, DropOffSite, Submission, StaffTransaction

from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, Sum, Exists, OuterRef

import uuid, json, requests
import cv2
import numpy as np
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from collections import Counter

from datetime import timedelta
from django.db.models.functions import Lower
from .services.reloadly import (
    get_reloadly_balance,
    list_reloadly_transactions,
    send_topup,
    auto_detect_operator,
    ReloadlyError,   # ← add this
)



# ------------------ DASHBOARD ------------------
def dashboard(request):
    reloadly_balance = None
    reloadly_currency = "PHP"
    reloadly_error = None
    try:
        bal = get_reloadly_balance()
        reloadly_balance = bal.get("balance") or bal.get("availableBalance") or bal.get("amount")
        reloadly_currency = bal.get("currencyCode") or bal.get("currency") or "PHP"
    except Exception as e:
        reloadly_error = f"Unable to fetch Reloadly balance: {e}"

    context = {
        'total_rewards': RewardRequest.objects.count(),
        'reloadly_balance': reloadly_balance,
        'reloadly_currency': reloadly_currency,
        'reloadly_error': reloadly_error,
    }
    return render(request, 'dashboard.html', context)

# ------------------ REWARD MANAGEMENT ------------------
# ------------------ RELOADLY (Airtime Top-up) ------------------

@api_view(['POST'])
@csrf_exempt
def redeem_reward_api(request):
    """
    Redeem points for PH mobile load using Reloadly (sandbox/live based on settings).
    Creates a RewardRequest and performs a top-up.
    """
    try:
        data = request.data
        user_id = data.get('user_id')
        phone_number = (data.get('phone_number') or "").strip()
        amount_str = str(data.get('amount') or "")
        # 'telco' not required anymore (Reloadly auto-detects operator)

        if not all([user_id, phone_number, amount_str]):
            return Response({'success': False, 'error': 'Missing required fields.'}, status=400)

        user = get_object_or_404(User, pk=user_id)

        # Parse amount (supports '₱50' or '50')
        digits = ''.join(ch for ch in amount_str if ch.isdigit())
        if not digits:
            return Response({'success': False, 'error': 'Invalid amount format.'}, status=400)
        amount_value = int(digits)  # pesos

        # Points: 1.00 point per ₱1.00 (your current logic: *100)
        points_cost = amount_value * 100
        if user.total_points < points_cost:
            return Response({
                'success': False,
                'error': f"You need {points_cost/100.0:.2f} points, but you only have {user.total_points/100.0:.2f}."
            }, status=400)

        # Create reward request (pending)
        reward_request = RewardRequest.objects.create(
            user=user,
            mobile_number=phone_number,
            telco="",  # optional now; operator is auto-detected by Reloadly
            points_used=points_cost,
            amount=amount_value,
            status='pending',
            date_requested=timezone.now()
        )

        # ---- Call Reloadly (auto-detect operator -> topup) ----
        # This returns the transaction payload (sandbox/live).
        try:
            tx = send_topup(phone=phone_number, amount=float(amount_value))
        except ReloadlyError as e:
            reward_request.status = 'failed'
            reward_request.response_note = f"Reloadly error: {e}"
            reward_request.save(update_fields=['status', 'response_note'])
            return Response({'success': False, 'error': str(e)}, status=400)

        # Typical fields: transactionId, status (SUCCESSFUL|PROCESSING|PENDING|FAILED)
        txn_id = tx.get('transactionId') or tx.get('id')
        status = (tx.get('status') or '').upper()

        reward_request.payrex_txn_id = txn_id  # reuse the same field to avoid DB changes
        reward_request.response_note = f"Reloadly status: {status}"
        reward_request.date_processed = timezone.now()

        if status in ('SUCCESSFUL', 'SUCCESS', 'COMPLETED'):
            # Top-up already completed -> approve + deduct points
            user.total_points -= points_cost
            user.save(update_fields=['total_points'])

            reward_request.status = 'approved'
            reward_request.save(update_fields=[
                'status', 'payrex_txn_id', 'response_note', 'date_processed'
            ])

            return Response({
                'success': True,
                'message': 'Load sent successfully!',
                'new_total_points': user.total_points
            }, status=201)

        elif status in ('PENDING', 'PROCESSING', 'REQUESTED'):
            # Accepted and processing at operator -> mark processing + deduct points
            user.total_points -= points_cost
            user.save(update_fields=['total_points'])

            reward_request.status = 'processing'
            reward_request.save(update_fields=[
                'status', 'payrex_txn_id', 'response_note', 'date_processed'
            ])

            return Response({
                'success': True,
                'message': 'Your load request is being processed.',
                'new_total_points': user.total_points
            }, status=201)

        else:
            # FAILED or unknown status
            reward_request.status = 'failed'
            reward_request.save(update_fields=[
                'status', 'payrex_txn_id', 'response_note', 'date_processed'
            ])
            return Response({'success': False, 'error': f'Topup status: {status or "UNKNOWN"}'}, status=400)

    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({'success': False, 'error': 'A server error occurred.'}, status=500)


@csrf_exempt
def reloadly_webhook(request):
    """
    OPTIONAL: If you enable Reloadly webhooks, handle final status here.
    Update RewardRequest by transactionId to approved/failed, refund points on failure.
    (Implement signature verification if you enable signing in Reloadly.)
    """
    try:
        payload = json.loads(request.body.decode('utf-8'))
        txn_id = payload.get('transactionId') or payload.get('id')
        status = (payload.get('status') or '').upper()

        if not txn_id:
            return HttpResponse(status=400)

        rr = RewardRequest.objects.filter(payrex_txn_id=txn_id).first()
        if not rr:
            return HttpResponse(status=200)  # nothing to do

        if status in ('SUCCESSFUL', 'SUCCESS', 'COMPLETED'):
            if rr.status != 'approved':
                rr.status = 'approved'
                rr.response_note = 'Reloadly webhook: top-up completed.'
                rr.save(update_fields=['status', 'response_note'])

        elif status in ('FAILED', 'ERROR'):
            if rr.status != 'failed':
                rr.status = 'failed'
                rr.response_note = 'Reloadly webhook: top-up failed.'
                rr.save(update_fields=['status', 'response_note'])

                # refund points if they were already deducted (processing path)
                u = rr.user
                u.total_points += rr.points_used
                u.save(update_fields=['total_points'])

        # ignore other statuses
        return HttpResponse(status=200)

    except Exception as e:
        print("Reloadly webhook error:", e)
        return HttpResponse(status=400)
    


def reward_requests_view(request):
    # Avoid N+1 on user lookups
    reward_requests = (
        RewardRequest.objects.select_related('user')
        .order_by('-id')
    )

    # Attach a canonical reference for the template (works even if some fields don't exist)
    for r in reward_requests:
        r.reloadly_ref = (
            getattr(r, "reloadly_topup_id", None)
            or getattr(r, "reloadly_txn_id", None)
            or getattr(r, "reloadly_transaction_id", None)
            or getattr(r, "reloadly_reference", None)
            or getattr(r, "payrex_txn_id", None)  # fallback while migrating
        )

    # Reloadly wallet + transactions
    reloadly_balance = None
    reloadly_currency = "PHP"
    reloadly_error = None
    reloadly_txns = []

    # Balance
    try:
        bal = get_reloadly_balance()
        if isinstance(bal, dict):
            reloadly_balance = bal.get("balance") or bal.get("availableBalance") or bal.get("amount")
            reloadly_currency = bal.get("currencyCode") or bal.get("currency") or "PHP"
        else:
            # If the helper returns a plain number
            reloadly_balance = bal
    except Exception as e:
        reloadly_error = f"Unable to fetch Reloadly balance: {e}"

    # Transactions (defensive against different shapes)
    try:
        tx = list_reloadly_transactions(page=1, size=20)
        if isinstance(tx, dict):
            reloadly_txns = (
                tx.get("content")
                or tx.get("data")
                or tx.get("transactions")
                or tx.get("items")
                or []
            )
        elif isinstance(tx, list):
            reloadly_txns = tx
        else:
            reloadly_txns = []
    except Exception as e:
        reloadly_error = (reloadly_error + " | " if reloadly_error else "") + f"Txns error: {e}"

    context = {
        "reward_requests": reward_requests,
        "total_rewards": RewardRequest.objects.count(),
        "reloadly_balance": reloadly_balance,
        "reloadly_currency": reloadly_currency,
        "reloadly_error": reloadly_error,
        "reloadly_txns": reloadly_txns,
    }
    return render(request, "reward_requests.html", context)


# ------------------ IMAGE ANALYSIS ------------------
@csrf_exempt
@api_view(['POST'])
@parser_classes([MultiPartParser])
def analyze_image(request):
    image_file = request.FILES.get('image')
    if not image_file:
        return JsonResponse({'error': 'No image provided'}, status=400)

    file_bytes = np.asarray(bytearray(image_file.read()), dtype=np.uint8)
    img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if img is None:
        return JsonResponse({'error': 'Failed to decode image'}, status=400)

    img = cv2.resize(img, (640, 480))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.medianBlur(gray, 5)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bottle_counts = {'250ml': 0, '500ml': 0, '1L': 0, '1.5L+': 0}
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if h < 100 or w < 30:
            continue
        if h <= 150:
            bottle_counts['250ml'] += 1
        elif h <= 200:
            bottle_counts['500ml'] += 1
        elif h <= 260:
            bottle_counts['1L'] += 1
        else:
            bottle_counts['1.5L+'] += 1

    total_detected = sum(bottle_counts.values())
    confidence = round(min(1.0, total_detected / 5.0), 2)
    suggested_size = max(bottle_counts, key=bottle_counts.get) if total_detected > 0 else "unknown"

    return JsonResponse({
        'total_detected': total_detected,
        'confidence_score': confidence,
        'suggested_size': suggested_size,
        'bottle_sizes': bottle_counts
    })

# ------------------ QUIZ MANAGEMENT ------------------
def quiz_dashboard(request):
    """
    A simplified dashboard for the admin to manage one single pool of questions.
    It no longer deals with categories.
    """
    # Fetch all questions from the single pool, ordered by newest first.
    all_questions = Quiz.objects.all().order_by('-id')
    
    # We only need the form for adding/editing questions.
    form = ManualQuizForm()

    context = {
        'questions': all_questions,
        'form': form,
    }
    return render(request, 'quiz_dashboard.html', context)

@csrf_exempt
@require_POST
def update_question(request, quiz_id):
    """
    Updates an existing quiz question. This view's logic remains mostly the same,
    but it's now simpler as it doesn't need to handle categories.
    """
    quiz = get_object_or_404(Quiz, id=quiz_id)
    form = ManualQuizForm(request.POST, instance=quiz)

    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    else:
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)

@csrf_exempt
@require_POST
def delete_question(request, question_id):
    """
    Deletes a question from the pool. This view is unchanged.
    """
    try:
        Quiz.objects.filter(id=question_id).delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
@require_POST
def create_quiz(request):
    """
    Creates a new quiz question (of any type) and adds it to the pool.
    This view now handles all question creation.
    """
    form = ManualQuizForm(request.POST)
    if form.is_valid():
        form.save() # The form is now simple and can be saved directly.
        return JsonResponse({'success': True})
    
    return JsonResponse({'success': False, 'errors': form.errors}, status=400)


# ------------------ STAFF MANAGEMENT & APPROVAL ------------------
def dropoff_sites_view(request):
    sites = DropOffSite.objects.select_related('assigned_staff')
    return render(request, 'dropoff_sites.html', {'sites': sites})

def staff_requests_view(request):
    pending_staff = User.objects.filter(user_type='staff', is_approved=False)
    return render(request, 'staff_requests.html', {'pending_staff': pending_staff})

@csrf_exempt
@require_POST
def approve_staff(request, user_id):
    staff = get_object_or_404(User, pk=user_id)

    if staff.user_type != 'staff':
        return JsonResponse({'success': False, 'error': 'User is not staff'})

    # ✅ Approve staff
    staff.is_approved = True
    staff.account_status = 'active'
    staff.save()

    # ✅ Assign to drop-off site
    site, created = DropOffSite.objects.get_or_create(barangay=staff.barangay)
    site.assigned_staff = staff
    site.save()

    # ✅ Prepared SMS (optional)
    send_sms_semaphore(
        staff.mobile_number,
        f"Hi {staff.name}, your RecyClean staff account has been approved. "
        f"You can now manage the drop-off site in {staff.barangay}."
    )

    # ✅ Return JSON with staff details for frontend insertion
    return JsonResponse({
        'success': True,
        'staff': {
            'id': staff.id,
            'name': staff.name,
            'barangay': staff.barangay,
            'mobile': staff.mobile_number
        }
    })


def send_sms_semaphore(mobile_number, message):
    """
    Prepares to send SMS via Semaphore. This won't send anything until SEMAPHORE_API_KEY is set.
    """
    try:
        import requests

        if not settings.SEMAPHORE_API_KEY:
            print("🔕 SMS not sent: No API key provided.")
            return False

        url = "https://api.semaphore.co/api/v4/messages"
        payload = {
            "apikey": settings.SEMAPHORE_API_KEY,
            "number": mobile_number,
            "message": message,
            "sendername": "RecyClean"
        }

        response = requests.post(url, data=payload)
        if response.status_code == 200:
            print("✅ SMS sent successfully.")
        else:
            print("❌ SMS sending failed:", response.text)

        return response.status_code == 200
    except Exception as e:
        print("📵 SMS error (will not crash system):", e)
        return False
    

@csrf_exempt
def staff_signup_api(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode('utf-8'))

            name = f"{data['first_name']} {data['last_name']}"
            email = data['email']
            mobile = data['phone']
            password = data['password']  # Raw password from request
            barangay = data['barangay']

            # ✅ Check duplicate mobile number
            if User.objects.filter(mobile_number=mobile, user_type='staff').exists():
                return JsonResponse({'success': False, 'error': 'Mobile number already exists'}, status=400)

            # ✅ Create staff with hashed password
            User.objects.create(
                name=name,
                email=email,
                mobile_number=mobile,
                password_hash=make_password(password),  # Hash before saving
                user_type='staff',
                barangay=barangay,
                is_approved=False,  # Must be approved by admin
                date_joined=timezone.now(),
                account_status='pending',
                registered_by_admin=False
            )

            return JsonResponse({'success': True, 'message': 'Application submitted'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
        
        

@csrf_exempt
@require_POST
def staff_login_view(request):
    try:
        data = json.loads(request.body.decode('utf-8'))
        identifier = data.get("identifier")  # Can be email or mobile
        password = data.get("password")

        if not identifier or not password:
            return JsonResponse({"success": False, "error": "Missing credentials."})

        # Find staff by email or phone
        user = User.objects.filter(user_type='staff').filter(
            Q(email=identifier) | Q(mobile_number=identifier)
        ).first()

        if not user:
            return JsonResponse({"success": False, "error": "Staff account not found or incorrect credentials."})

        # 🚫 If not approved
        if not user.is_approved:
            return JsonResponse({
                "success": False,
                "error": "Your account is still pending approval. Please wait for the admin to approve it."
            })

        # ✅ Verify hashed password
        if not check_password(password, user.password_hash):
            return JsonResponse({"success": False, "error": "Incorrect password."})

        # ✅ Success
        return JsonResponse({
            "success": True,
            "message": "Login successful.",
            "is_approved": user.is_approved,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "mobile": user.mobile_number,
                "barangay": user.barangay,
            }
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})

@csrf_exempt
@require_POST
def reject_staff(request, user_id):
    staff = get_object_or_404(User, pk=user_id)

    if staff.user_type != 'staff':
        return JsonResponse({'success': False, 'error': 'User is not staff'})

    name = staff.name
    mobile = staff.mobile_number

    # Send SMS before deletion
    send_sms_semaphore(
        mobile,
        f"Hi {name}, unfortunately your RecyClean staff application has been rejected. Thank you for your interest."
    )

    staff.delete()

    return JsonResponse({'success': True})


# ------------------ DROP-OFF SITE DETAIL ------------------  # 🔁 Add this at the top if not already imported

@api_view(['POST'])
@csrf_exempt
def process_qr_submission(request):
    data = request.data
    print("📥 Incoming submission data:", data)

    qr_id = data.get('qr_id')
    if not qr_id:
        return Response({'success': False, 'error': 'Missing QR ID'}, status=400)

    try:
        # 🚫 Prevent reuse
        if Submission.objects.filter(qr_id=qr_id).exists():
            return Response({'success': False, 'error': 'QR code already used'}, status=400)

        # ✅ Optional staff
        staff = None
        if data.get('staff_id'):
            staff = User.objects.filter(id=data['staff_id'], user_type='staff').first()
            if not staff:
                return Response({'success': False, 'error': f"Staff with id {data['staff_id']} not found"}, status=404)

        # 🧠 Normalize barangay and match drop-off site
        dropoff_name = data.get('dropoff_site', '').strip()
        dropoff_site = DropOffSite.objects.annotate(
            normalized_name=Lower('barangay')
        ).filter(normalized_name=dropoff_name.lower()).first()

        if not dropoff_site:
            return Response({'success': False, 'error': f"Drop-off site '{dropoff_name}' not found"}, status=404)

        # ✅ Optional user
        user = None
        if data.get('user_id'):
            user = User.objects.filter(id=data['user_id'], user_type='resident').first()
            if not user:
                return Response({'success': False, 'error': f"Resident with id {data['user_id']} not found"}, status=404)

        bottles = data.get('bottles', [])
        total_points = data.get('total_points', 0)

        # ✅ Save submission
        submission = Submission.objects.create(
            qr_id=qr_id,
            user=user,
            staff=staff,
            dropoff_site=dropoff_site,
            bottle_data=bottles,
            total_points=total_points,
            source='manual'
        )

        # ✅ Staff transaction (user might be None)
        if staff:
            StaffTransaction.objects.create(
                staff=staff,
                submission=submission,
                action='manual_submission',
                notes=f"Manual submission for {user.name}" if user else "Manual submission (no user linked)"
            )

        # ✅ Handle weekly bonus logic
        bonus_awarded = False
        quota_progress = 0.0

        if user:
            user.total_points += total_points

            WEEKLY_TARGET = 50
            BONUS_POINTS = 5
            today = timezone.now().date()
            start_of_week = today - timedelta(days=today.weekday())

            weekly_subs = Submission.objects.filter(
                user=user,
                created_at__date__gte=start_of_week
            )

            bottles_this_week = 0
            for sub in weekly_subs:
                if isinstance(sub.bottle_data, list):
                    for bottle in sub.bottle_data:
                        bottles_this_week += bottle.get('quantity', 0)

            for bottle in bottles:
                bottles_this_week += bottle.get('quantity', 0)

            quota_progress = min(bottles_this_week / WEEKLY_TARGET, 1.0)

            if quota_progress >= 1.0 and (not user.weekly_bonus_given or user.weekly_bonus_given != today):
                user.total_points += BONUS_POINTS
                user.weekly_bonus_given = today
                bonus_awarded = True

            user.save()

        return Response({
            "success": True,
            "message": "Submission saved.",
            "new_points": user.total_points if user else 0,
            "quota_progress": quota_progress,
            "bonus": bonus_awarded
        }, status=201)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response({'success': False, 'error': str(e)}, status=400)
    
@csrf_exempt
@require_POST
def delete_dropoff_site(request, site_id):
    try:
        site = get_object_or_404(DropOffSite, pk=site_id)
        site.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    

# ------------------ STAFF TRANSACTION HISTORY ------------------

def submissions_by_dropoff_site(request, site_id):
    site = get_object_or_404(DropOffSite, id=site_id)
    submissions = Submission.objects.filter(dropoff_site=site).select_related('staff', 'user').order_by('-created_at')
    return render(request, 'submissions_by_site.html', {
        'site': site,
        'submissions': submissions
    })

@csrf_exempt
def staff_metrics(request, staff_id):
    try:
        staff = User.objects.get(pk=staff_id, user_type='staff')

        today = timezone.now().date()
        past_week = [today - timedelta(days=i) for i in range(6, -1, -1)]

        daily_data = []
        for day in past_week:
            count = Submission.objects.filter(
                dropoff_site=staff.dropoff_site,
                created_at__date=day
            ).count()
            daily_data.append({"day": day.strftime("%a"), "count": count})

        monthly_start = today.replace(day=1)
        monthly_submissions = Submission.objects.filter(
            dropoff_site=staff.dropoff_site,
            created_at__date__gte=monthly_start
        )

        monthly_bottle_count = 0
        for s in monthly_submissions:
            for b in s.bottle_data:
                monthly_bottle_count += b.get('quantity', 0)

        return JsonResponse({
            "success": True,
            "staff": {
                "id": staff.id,
                "name": staff.name,
                "barangay": staff.barangay
            },
            "daily_breakdown": [
                {"day": d["day"], "count": d["count"]} for d in daily_data
            ],
            "weekly_submissions": sum(d["count"] for d in daily_data),
            "monthly_plastic_kg": monthly_bottle_count
        })
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "Staff not found"}, status=404)
    
    
@api_view(['GET'])
def staff_transaction_history(request, staff_id):
    transactions = StaffTransaction.objects.filter(staff_id=staff_id).order_by('-created_at')
    data = []
    for t in transactions:
        data.append({
            "action": t.action,
            "notes": t.notes,
            "points": t.submission.total_points,
            "date": t.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return Response({"success": True, "transactions": data})
 
 
# Data Visualization for Drop-off Site Detail
def dropoff_site_detail(request, site_id):
    site = get_object_or_404(DropOffSite, id=site_id)
    submissions = Submission.objects.filter(
        dropoff_site=site
    ).select_related('staff', 'user').order_by('-created_at')

    # 📊 Metrics
    total_submissions = submissions.count()
    total_points = submissions.aggregate(total=Sum('total_points'))['total'] or 0
    total_bottles = sum(
        sum(bottle.get('quantity', 0) for bottle in sub.bottle_data)
        for sub in submissions
    )

    # 📈 Daily submissions for last 7 days
    today = timezone.now().date()
    last_7_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    daily_counts = []
    for day in last_7_days:
        count = submissions.filter(created_at__date=day).count()
        daily_counts.append({
            'date': day.strftime("%b %d"),
            'count': count
        })

    # 🥤 Bottle size distribution
    bottle_counter = Counter()
    for sub in submissions:
        for bottle in sub.bottle_data:
            size = bottle.get('size', 'Unknown')
            qty = bottle.get('quantity', 0)
            bottle_counter[size] += qty

    bottle_labels = list(bottle_counter.keys())
    bottle_values = list(bottle_counter.values())

    return render(request, 'dropoff_site_detail.html', {
        'site': site,
        'submissions': submissions,
        'total_submissions': total_submissions,
        'total_points': total_points,
        'total_bottles': total_bottles,
        # Chart.js Data
        'daily_labels': json.dumps([d['date'] for d in daily_counts]),
        'daily_values': json.dumps([d['count'] for d in daily_counts]),
        'bottle_labels': json.dumps(bottle_labels),
        'bottle_values': json.dumps(bottle_values),
    })

def staff_management_view(request):
    pending_staff = User.objects.filter(user_type='staff', is_approved=False)
    active_staff = User.objects.filter(user_type='staff', is_approved=True)

    return render(request, 'staff_management.html', {
        'pending_staff': pending_staff,
        'active_staff': active_staff
    })


@api_view(['GET'])
def get_staff_transactions(request, staff_id):
    transactions = StaffTransaction.objects.filter(staff_id=staff_id).order_by('-created_at')
    data = [
        {
            "date": t.created_at.strftime("%Y-%m-%d %H:%M"),
            "points": t.submission.total_points,
            "notes": t.notes
        }
        for t in transactions
    ]
    return Response({"success": True, "transactions": data})


@csrf_exempt
@require_POST
def delete_staff(request, staff_id):
    try:
        staff = get_object_or_404(User, pk=staff_id, user_type='staff')

        # Delete all submissions linked to this staff (transactions will auto-delete if cascade is set in StaffTransaction)
        Submission.objects.filter(staff=staff).delete()

        # Finally, delete the staff account
        staff.delete()

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    

# ------------------RESIDENT MANAGEMENT ------------------

@csrf_exempt
@require_POST
def resident_signup_api(request):
    try:
        data = json.loads(request.body.decode('utf-8'))

        name = f"{data['first_name']} {data['last_name']}"
        email = data['email']
        password = data['password']

        # CORRECTED: Default to None instead of an empty string
        mobile_number = data.get('mobile', None)

        # Check if email already exists
        if User.objects.filter(email=email, user_type='resident').exists():
            return JsonResponse({'success': False, 'error': 'An account with this email already exists.'}, status=400)

        # NEW: Check for mobile number duplicate only if one is provided
        if mobile_number and User.objects.filter(mobile_number=mobile_number).exists():
            return JsonResponse({'success': False, 'error': 'An account with this mobile number already exists.'}, status=400)

        # Create resident
        User.objects.create(
            name=name,
            email=email,
            mobile_number=mobile_number, # This will now be None if not provided
            password_hash=make_password(password),
            user_type='resident',
            barangay="",
            is_approved=True,
            date_joined=timezone.now(), # Use timezone.now() for consistency
            account_status='active',
            registered_by_admin=False
        )

        return JsonResponse({'success': True, 'message': 'Account created successfully'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def resident_login_api(request):
    try:
        data = json.loads(request.body.decode('utf-8'))
        email = data.get("email")
        password = data.get("password")

        if not email or not password:
            return JsonResponse({"success": False, "error": "Missing credentials."}, status=400)

        user = User.objects.filter(email=email, user_type='resident').first()
        if not user:
            return JsonResponse({"success": False, "error": "Account not found."}, status=404)

        # ✅ Check password
        if not check_password(password, user.password_hash):
            return JsonResponse({"success": False, "error": "Incorrect password."}, status=400)

        # ✅ Calculate total points (in case it's stored in a different way)
        total_points = getattr(user, "total_points", 0)

        return JsonResponse({
            "success": True,
            "message": "Login successful.",
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "points": total_points
            }
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
    

@csrf_exempt
@require_GET
def get_user_details(request, user_id):
    """
    Returns user details for the mobile dashboard & profile.
    Includes name, total points, and weekly quota progress.
    """
    try:
        user = User.objects.filter(pk=user_id, user_type='resident').first()
        if not user:
            return JsonResponse({"success": False, "error": "User not found"}, status=404)

        total_points = getattr(user, "total_points", 0) or 0
        WEEKLY_TARGET = 50  # bottles per week for 100% quota

        start_of_week = timezone.now().date() - timedelta(days=timezone.now().weekday())
        weekly_submissions = Submission.objects.filter(
            user=user,
            created_at__date__gte=start_of_week
        )

        bottles_this_week = 0
        for sub in weekly_submissions:
            if isinstance(sub.bottle_data, list):
                for bottle in sub.bottle_data:
                    bottles_this_week += bottle.get('quantity', 0)

        quota_progress = min(bottles_this_week / WEEKLY_TARGET, 1.0) if WEEKLY_TARGET > 0 else 0.0

        return JsonResponse({
            "success": True,
            "id": user.id,
            "name": user.name,
            "points": total_points,
            "quota_progress": quota_progress
        }, status=200)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)

# ------------------ QUIZ API FOR FLUTTER APP ------------------
# ------------------ REVISED QUIZ API (FOR FLUTTER APP) ------------------

def _get_quiz_availability(user_id):
    """
    A helper function that checks daily/weekly quiz completion for a user.
    Returns a dictionary with availability status. This prevents code duplication.
    """
    user = get_object_or_404(User, pk=user_id)
    today = timezone.now().date()
    start_of_week = today - timezone.timedelta(days=today.weekday())

    daily_completed = QuizSession.objects.filter(
        user=user, quiz_type='daily', completed_at__date=today
    ).exists()

    weekly_completed = QuizSession.objects.filter(
        user=user, quiz_type='weekly', completed_at__date__gte=start_of_week
    ).exists()

    return {
        "daily_available": not daily_completed,
        "weekly_available": not weekly_completed,
    }


@api_view(['GET'])
@csrf_exempt
def get_quiz_status(request, user_id):
    """
    API Endpoint for Flutter: Checks if the daily and weekly quizzes are available.
    """
    try:
        # This view now simply calls the helper function and returns its result.
        availability_data = _get_quiz_availability(user_id)
        return JsonResponse({"success": True, **availability_data})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@api_view(['GET'])
@csrf_exempt
def get_quiz_questions(request, quiz_type, user_id):
    """
    API Endpoint for Flutter: Gets a random set of questions for a quiz.
    """
    try:
        # This view also calls the helper function for validation.
        availability_data = _get_quiz_availability(user_id)

        if quiz_type == 'daily' and not availability_data.get('daily_available'):
            return JsonResponse({"success": False, "error": "Daily quiz already completed."}, status=403)
        if quiz_type == 'weekly' and not availability_data.get('weekly_available'):
            return JsonResponse({"success": False, "error": "Weekly quiz already completed."}, status=403)

        if quiz_type == 'daily':
            question_count = 15
        elif quiz_type == 'weekly':
            question_count = 25
        else:
            return JsonResponse({"success": False, "error": "Invalid quiz type."}, status=400)

        questions = Quiz.objects.order_by('?').all()[:question_count]
        
        question_data = list(questions.values(
            'id', 'question_type', 'question', 'option_a', 
            'option_b', 'option_c', 'option_d'
        ))

        return JsonResponse({"success": True, "questions": question_data})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@api_view(['POST'])
@csrf_exempt
def submit_quiz_answers(request):
    """
    Submits answers, calculates a precise score, and awards points.
    Includes bonus points for a perfect daily quiz.
    NOTE: Points are scaled by 100 to handle fractions as integers (e.g., 5 points = 500).
    """
    try:
        data = request.data
        user_id = data.get('user_id')
        quiz_type = data.get('quiz_type')
        answers = data.get('answers', [])

        user = get_object_or_404(User, pk=user_id)

        # --- Scoring Logic ---
        correct_answers_count = 0
        total_questions = len(answers)
        if total_questions == 0:
            return JsonResponse({"success": False, "error": "No answers provided."}, status=400)

        question_ids = [ans['question_id'] for ans in answers]
        correct_answers_map = {q.id: q for q in Quiz.objects.filter(id__in=question_ids)}

        for answer in answers:
            question = correct_answers_map.get(answer['question_id'])
            if question and question.correct_option == answer.get('selected_option'):
                correct_answers_count += 1
        
        # --- NEW & PRECISE: Points Calculation Logic ---
        base_points_awarded = 0
        bonus_points_awarded = 0
        is_perfect_score = (correct_answers_count == total_questions)

        # Define max points for each quiz type (scaled by 100)
        SCALING_FACTOR = 100
        MAX_POINTS_DAILY = 5 * SCALING_FACTOR  # 500 units
        MAX_POINTS_WEEKLY = 25 * SCALING_FACTOR # 2500 units

        if quiz_type == 'daily':
            # Calculate points based on performance: (correct / total) * max_points
            base_points_awarded = (correct_answers_count * MAX_POINTS_DAILY) // total_questions
            
            # Add bonus for a perfect score
            if is_perfect_score:
                bonus_points_awarded = 5 * SCALING_FACTOR # 500 bonus units
        
        elif quiz_type == 'weekly':
            # You can apply the same precision here if you want
            base_points_awarded = (correct_answers_count * MAX_POINTS_WEEKLY) // total_questions
            # Optional: Add a weekly bonus for perfect score
            # if is_perfect_score:
            #     bonus_points_awarded = 10 * SCALING_FACTOR

        total_points_to_award = base_points_awarded + bonus_points_awarded

        # --- Update User and Session ---
        # The user's total_points is an integer, so this works perfectly.
        user.total_points += total_points_to_award
        user.save(update_fields=['total_points'])

        QuizSession.objects.create(
            user=user,
            quiz_type=quiz_type,
            correct_answers=correct_answers_count,
            total_questions=total_questions,
            points_awarded=total_points_to_award # Store the scaled integer
        )

        # --- Return detailed result to Flutter App ---
        # We send the scaled integers. The app will be responsible for formatting.
        return JsonResponse({
            "success": True,
            "message": "Quiz submitted successfully!",
            "correct_answers": correct_answers_count,
            "total_questions": total_questions,
            "base_points_awarded": base_points_awarded,
            "bonus_points_awarded": bonus_points_awarded,
            "total_points_awarded": total_points_to_award,
        }, status=201)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({"success": False, "error": str(e)}, status=500)