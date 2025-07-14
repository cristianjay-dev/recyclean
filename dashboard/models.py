from django.db import models


class User(models.Model):
    name = models.CharField(max_length=100)
    total_points = models.IntegerField(default=0)
    
class Submission(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    quantity = models.IntegerField()
    timestamp = models.DateTimeField(auto_now_add=True)

class RewardRequest(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    mobile_number = models.CharField(max_length=15)
    load_amount = models.IntegerField()
    status = models.CharField(max_length=10, choices=[('pending', 'Pending'), ('approved', 'Approved')])
    


class DropOffPoint(models.Model):
    name = models.CharField(max_length=100)
    address = models.TextField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    qr_code = models.ImageField(upload_to='qr_codes/', blank=True)

    def __str__(self):
        return self.name

