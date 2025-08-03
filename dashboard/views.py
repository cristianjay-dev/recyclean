from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.middleware.csrf import get_token

from .forms import ManualQuizForm, QuizCategoryForm
from .models import RewardRequest, QuizCategory, Quiz, QuizAnswer, User, QuizSession, DropOffSite
from .payrex_api import get_balance

from django.views.decorators.http import require_POST

import json
from django.utils.timezone import now
from django.db.models import Q

import uuid, json
import cv2
import numpy as np
from rest_framework.parsers import MultiPartParser
from rest_framework.decorators import api_view, parser_classes

# ------------------ DASHBOARD ------------------
def dashboard(request):
    balance_data = get_balance()
    context = {
        'total_rewards': RewardRequest.objects.count(),
        'payrex_balance': balance_data.get("balance") if balance_data else None,
    }
    return render(request, 'dashboard.html', context)

# ------------------ REWARD MANAGEMENT ------------------
def reward_requests_view(request):
    reward_requests = RewardRequest.objects.all().order_by('-id')
    balance_data = get_balance()

    return render(request, 'reward_requests.html', {
        'reward_requests': reward_requests,
        'payrex_balance': balance_data.get("balance") if balance_data else None,
        'payrex_error': None if balance_data else "Unable to fetch balance"
    })

@csrf_exempt
def approve_reward(request, pk):
    if request.method == 'POST':
        req = get_object_or_404(RewardRequest, pk=pk)
        if req.status == 'pending':
            req.status = 'approved'
            req.payrex_txn_id = f"TEST-{uuid.uuid4().hex[:8]}"
            req.response_note = "Simulated load delivery successful."
            req.save()
    return redirect('reward_requests')

# ------------------ PAYREX API ------------------
def payrex_balance_view(request):
    url = "https://api.payrex.ph/balance"
    headers = {
        "Authorization": f"Bearer {settings.PAYREX_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return JsonResponse({"balance": data.get("balance", "unknown")})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def payrex_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
        event_type = payload.get("type", "")

        if event_type == "payout.deposited":
            data = payload.get("data", {})
            mobile = data.get("mobile_number")
            amount = data.get("amount")

            RewardRequest.objects.filter(
                mobile_number=mobile,
                load_amount=amount,
                status='pending'
            ).update(status='approved')

        return HttpResponse(status=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        return HttpResponse(status=400)

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

def quiz_dashboard(request):
    categories = QuizCategory.objects.all().prefetch_related('quiz_set')
    return render(request, 'quiz_dashboard.html', {
        'categories': categories,
        'form': ManualQuizForm(),
        'category_form': QuizCategoryForm()
    })

@csrf_exempt
@require_POST
def add_question_to_category(request, category_id):
    category = get_object_or_404(QuizCategory, id=category_id)
    form = ManualQuizForm(request.POST)

    if form.is_valid():
        quiz = form.save(commit=False)
        quiz.category = category
        quiz.save()
        return JsonResponse({'success': True})
    else:
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)

@csrf_exempt
@require_POST
def update_question(request, quiz_id):
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
    try:
        Quiz.objects.filter(id=question_id).delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
@require_POST
def delete_category(request, category_id):
    try:
        category = get_object_or_404(QuizCategory, id=category_id)
        if category.quiz_set.exists():
            return JsonResponse({'success': False, 'error': 'Cannot delete category with questions.'})
        category.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
@require_POST
def create_quiz(request):
    form = ManualQuizForm(request.POST)
    if form.is_valid():
        form.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'errors': form.errors}, status=400)

@csrf_exempt
@require_POST
def create_category(request):
    form = QuizCategoryForm(request.POST)
    if form.is_valid():
        form.save()
        return redirect('quiz_dashboard')
    return redirect('quiz_dashboard')


# Staff Approval

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

    staff.is_approved = True
    staff.account_status = 'active'
    staff.save()

    # Automatically assign staff to drop-off site
    site, created = DropOffSite.objects.get_or_create(barangay=staff.barangay)
    site.assigned_staff = staff
    site.save()

    # ✅ Prepared SMS logic, but won't send without API key
    send_sms_semaphore(
        staff.mobile_number,
        f"Hi {staff.name}, your RecyClean staff account has been approved. You can now manage the drop-off site in {staff.barangay}."
    )

    return JsonResponse({'success': True})


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
            password_hash = data['password']
            barangay = data['barangay']

            if User.objects.filter(mobile_number=mobile).exists():
                return JsonResponse({'success': False, 'error': 'Mobile number already exists'}, status=400)

            User.objects.create(
                name=name,
                email=email,
                mobile_number=mobile,
                password_hash=password_hash,  # 🔐 You may want to hash this
                user_type='staff',
                barangay=barangay,
                is_approved=False,
                date_joined=now(),
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
        identifier = data.get("identifier")  # can be email or mobile number
        password = data.get("password")

        if not identifier or not password:
            return JsonResponse({"success": False, "error": "Missing credentials."})

        # Find user by email or phone
        user = User.objects.filter(user_type='staff').filter(
            Q(email=identifier) | Q(mobile_number=identifier)
        ).first()

        if not user:
            return JsonResponse({"success": False, "error": "Staff account not found or incorrect credentials."})

        # 🚫 Check if not approved
        if not user.is_approved:
            return JsonResponse({
                "success": False,
                "error": "Your account is still pending approval. Please wait for the admin to approve it."
            })

        # 🔐 You may want to hash and compare passwords in production
        if user.password_hash != password:
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
