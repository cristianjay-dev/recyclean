"""
General Remarks:
- remove ID fields because it is already provided automatically by django
    data type of ID fields are indicated in settings.py --> DEFAULT_AUTO_FIELD
- structure models via  availability of ForeignKey fields 
    models with no FK field should be on top
- Make your custom user model inherit from django's standard user model: https://docs.djangoproject.com/en/5.2/topics/auth/customizing/
    After doing this, go to settings.py and add AUTH_MODEL = 'dashboard.User'


Extras:
- `User.user_type` can be removed and instead, use django's Group model
- Create Barangay Model
- Use https://nominatim.org/ for geolocation for complete barangay and automated
- 
"""

from django.db import models

# Imports by Jxst-Felix
from django.contrib.auth.models import AbstractUser as StandardUserModel # this is the standard User model of django, inherit this for your custom user model
from django.contrib.auth.models import Group # this is the Group model of django, you can assign this to users and you can assign certain permissions to users that belongs to this group


# -------------------------
# DROP-OFF SITE TABLE
# -------------------------
class DropOffSite(models.Model):
    id = models.AutoField(primary_key=True)
    barangay = models.CharField(max_length=100, unique=True)
    assigned_staff = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dropoff_site'
    )

    class Meta:
        managed = True
        db_table = 'dropoff_sites'

    def __str__(self):
        return f"{self.barangay} Drop-Off Site"

# -------------------------
# USER TABLE (Residents & Staff)
# -------------------------
class User(models.Model):
    """
    Redundant models if inheriting from standard django user model:
    - email
    - password hash --> password
    - date_joined
    ---

    Possible redundant fields:
    - is_approved --> is_staff
    """
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=255)
    mobile_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    password_hash = models.TextField()
    user_type = models.CharField(max_length=10)  # 'resident' or 'staff'
    barangay = models.CharField(max_length=100, blank=True)
    is_approved = models.BooleanField(default=False)
    date_joined = models.DateTimeField()
    account_status = models.CharField(max_length=10)  # 'active' or 'disabled'
    registered_by_admin = models.BooleanField()
    weekly_bonus_given = models.DateField(null=True, blank=True)
    total_points = models.IntegerField(default=0)

    class Meta:
        managed = True
        db_table = 'users'

    def __str__(self):
        return self.name

# -------------------------
# SUBMISSIONS TABLE
# -------------------------
class Submission(models.Model):
    """
    Possible improvements:
    - Use Many-to-Many fields on bottle_data
    ---
    
    Extras:
    - Use models.FileField for image_path
    - Remember what the pupose of estimated_quantity and confidence_score
    """
    # ... This model remains unchanged ...
    id = models.AutoField(primary_key=True)
    qr_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='submissions')
    staff = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='processed_submissions')
    dropoff_site = models.ForeignKey(DropOffSite, on_delete=models.SET_NULL, null=True, related_name='submissions')
    bottle_data = models.JSONField(default=list)
    total_points = models.IntegerField()
    source = models.CharField(max_length=20, default='manual')
    image_path = models.TextField(blank=True, null=True)
    estimated_quantity = models.IntegerField(blank=True, null=True)
    confidence_score = models.FloatField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'submissions'

    def __str__(self):
        user_name = self.user.name if self.user else "Unlinked User"
        return f"Submission for {user_name} - {self.total_points} pts"

# -------------------------
# STAFF TRANSACTION HISTORY TABLE
# -------------------------
class StaffTransaction(models.Model):
    # ... This model remains unchanged ...
    id = models.AutoField(primary_key=True)
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='staff_transactions')
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name='transaction_record')
    action = models.CharField(max_length=50, default='submission_made')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'staff_transactions'

# -------------------------
# REWARD REQUESTS TABLE
# -------------------------
class RewardRequest(models.Model):
    """
    Remarks:
    - change `amount` and `points_used`
    - Refactor model
    """
    # ... This model remains unchanged ...
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    mobile_number = models.CharField(max_length=20)
    telco = models.CharField(max_length=10)
    points_used = models.IntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10)
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='processed_rewards')
    date_requested = models.DateTimeField()
    date_processed = models.DateTimeField(null=True)

    class Meta:
        managed = True
        db_table = 'reward_requests'


# -------------------------
# QUIZ QUESTIONS TABLE (MODIFIED)
# -------------------------
class Quiz(models.Model):
    id = models.AutoField(primary_key=True)

    QUESTION_TYPE_CHOICES = [
        ('multiple_choice', 'Multiple Choice'),
        ('true_false', 'True/False'),
    ]
    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPE_CHOICES,
        default='multiple_choice'
    )
    
    question = models.TextField()

    # These fields are now optional, only used for 'multiple_choice'
    option_a = models.TextField(blank=True, null=True)
    option_b = models.TextField(blank=True, null=True)
    option_c = models.TextField(blank=True, null=True)
    option_d = models.TextField(blank=True, null=True)

    # For 'multiple_choice', this will be 'a', 'b', 'c', 'd'.
    # For 'true_false', this will be 'true' or 'false'.
    correct_option = models.CharField(max_length=10)
    
    # This is kept for potential future use or for admin reference
    points = models.IntegerField(default=1)

    class Meta:
        managed = True
        db_table = 'quizzes'

    def __str__(self):
        return self.question

# -------------------------
# QUIZ ANSWERS TABLE (MODIFIED)
# -------------------------
class QuizAnswer(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='quiz_answers')
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='user_answers')
    
    # Increased max_length to accommodate 'true'/'false'
    selected_option = models.CharField(max_length=10)
    is_correct = models.BooleanField()
    date_answered = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'quiz_answers'

# -------------------------
# QUIZ SESSION TABLE (MODIFIED)
# -------------------------
class QuizSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='quiz_sessions')
    
    QUIZ_TYPE_CHOICES = [
        ('daily', 'Daily Quiz'),
        ('weekly', 'Weekly Quiz'),
    ]
    quiz_type = models.CharField(max_length=10, choices=QUIZ_TYPE_CHOICES)
    
    correct_answers = models.IntegerField()
    total_questions = models.IntegerField()
    points_awarded = models.IntegerField(default=0)
    
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'quiz_sessions'

# -------------------------
# USER BADGES TABLE
# -------------------------
class UserBadge(models.Model):
    # ... This model remains unchanged ...
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='badges')
    badge_name = models.CharField(max_length=100)
    awarded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'user_badges'